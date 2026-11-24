# ADR-0003: the local storage model - raw payload retention, normalized series, watermarks

- **Status:** accepted
- **Date:** 2026-11-22
- **Implements:** `fitbit_monitor/store/` (`migrations/*.sql`, `schema.py`, `raw.py`,
  `writer.py`, `reader.py`, `maintenance.py`), `fm db`, `fm store`
- **Related:** ADR-0004 (plugin contract - the read API is a plugin's only window onto
  this schema), ADR-0005 (ad-hoc import - the writer of this schema in anger),
  `docs/storage.md` (user-facing reference), `docs/data-policy.md` (locality, retention)

## Context

Everything else in the program - import, plugins, anomaly detection, reports - reads
and writes through one local SQLite database. Three properties have to be decided once,
here, because changing them later means either a data-loss migration or a compatibility
layer forever:

1. **What is the record?** The API gave us bytes; a plugin wants a typed series; the
   user wants the answer to be re-derivable in five years, when this program's schema
   will certainly have proved wrong about something.
2. **What makes a re-run safe?** Imports overlap by accident and on purpose: `--since`
   from a watermark, a backfill from a CSV export of the same window, a re-run after an
   interruption. "Just run it again" has to be the correct answer, so idempotence is a
   schema property, not a behaviour.
3. **What is the read surface?** If plugins write SQL against the schema, the schema
   becomes public API and can never change again.

## Decision

### 1. The database file, and how it is opened

One SQLite file, `<data-dir>/store.sqlite3` (default
`$XDG_DATA_HOME/fitbit-monitor/store.sqlite3`), overridable with `--db`, `FM_DB` or
`[store] path` in the config. WAL journal mode, `PRAGMA foreign_keys=ON`, and
`PRAGMA busy_timeout=5000` on **every** open (`schema.connect` is the only way to get a
connection, which is what makes those three promises universal rather than local).
Every command writes through a `flock`-based single-writer lock next to the file.

A read-only handle is opened `file:…?mode=ro` **and** `PRAGMA query_only=ON`, so
"read-only" is enforced by SQLite rather than by manners; a plugin that digs down to
`ctx.store.conn.execute("DELETE …")` gets `OperationalError`.

### 2. Raw payloads are the record

`raw_payload` keeps the response body **byte-for-byte**, content-addressed:

| column | meaning |
|---|---|
| `content_hash` | SHA-256 of the body as received - `UNIQUE`, the dedup key |
| `dataset` | which dataset it was fetched for (`REFERENCES dataset(name)`) |
| `source` | `api`, `csv`, or a fixture directory |
| `source_id` | what identified the request (the API path, or the archive member) |
| `requested_start`, `requested_end` | the window that response was asked to cover |
| `fetched_at` | when it was fetched (UTC) |
| `bytes`, `stored_bytes` | size as received, size as stored |
| `encoding` | `gzip` or `identity` - every reader checks this before decompressing |
| `body` | the payload, gzip BLOB above 512 bytes and as it came below that |

**Why keep raw at all:** a normalizer is a guess about the present. If tomorrow we
learn that the sleep payload's `awakeningsCount` matters, or that a Fitbit revision
quietly renamed a field, the only honest recovery is to re-normalize what was actually
returned. Throwing raw away after parsing turns a schema mistake into permanent data
loss, and this is someone's heart-rhythm history. Compression is what makes keeping it
affordable: intraday JSON compresses roughly 8:1, so raw costs a fraction of the
normalized rows it came from. Small bodies (an empty day, an error response) are stored
uncompressed because gzip would make them *larger*.

**Blob storage decision: SQLite BLOB, not content-addressed files.** A directory of
`store/../raw/<hash>.json.gz` would keep the database small and the payloads
`rsync`-able, and it was seriously considered. It loses because it makes *one file* stop
being *one backup*: `fm store backup`, `PRAGMA integrity_check`, a transaction that
commits a payload and its rows together, and "copy one file to a USB stick" all stop
working, and a store whose sidecar directory was half-copied is a silently corrupt
archive. The file lives inside the database, is hashed by `verify`, and is exported with
it. If the store ever grows past what a single file can carry, the escape hatch is to add
a `raw_file` side-table pointing at content-addressed files - additive, no rewrite.

Because the unique key is the hash alone, a payload two overlapping imports both
fetched costs the store **one row**, no matter which dataset or range asked for it.

### 3. One normalized table, one typed view per dataset

`series` is the normalized store:

| column | meaning |
|---|---|
| `dataset` | e.g. `heart_rate`, `daily_summary`, `hrv`, `sleep` |
| `ts_utc` | the instant, ISO-8601 UTC text with an explicit `+00:00` |
| `payload` | JSON object of the numeric fields recorded at that instant |
| `source_id` | what last wrote it |

Primary key `(dataset, ts_utc)` **is** the idempotence key: an overlapping import of
the same minute lands on the same key and is patched (`json_patch`) rather than
duplicated. `series_dataset_day (dataset, substr(ts_utc, 1, 10))` and the primary key
cover the only query that matters - "all of dataset X between t1 and t2" - and
`raw_payload(dataset, fetched_at)` does the same for the raw side.

`dataset` is a registry, not a wish-list: `name`, `kind` (`intraday` / `daily`),
`granularity_seconds`, `first_seen_at`. The writer registers a dataset the first time
it stores anything for it, which is what lets `fm store stats` and
`ReadOnlyStore.stored_datasets()` answer "what is in here" without scanning data, and
gives `raw_payload` a foreign key to point at.

**Rejected: per-dataset physical tables** (`heart_rate_intraday`, `daily_summary`,
`sleep`, `hrv` as four tables). The ticket asked for them; they were rejected on the
merits, and the reason is recorded here so nobody re-litigates it. Four tables mean four
normalization paths and two sources of truth (the payload JSON *and* the columns), a
migration every time a field appears, a `dataset` column on every write for a dataset
the table name already fixed, and - worst - an import that can half-succeed by writing
rows to one place and not the other. The typed columnar surface the ticket actually
wanted is delivered by **SQL views** (`v_heart_rate_intraday`, `v_daily_summary`,
`v_sleep`, `v_hrv`) that extract the fields with names and units (`heart_rate_bpm`,
`rmssd_ms`, …) at zero storage cost and one writer path. Uniqueness keys come from the
primary key, exactly as the ticket requires.

**Which fields survive normalization:** every numeric field the dataset's normalizer
names is stored, with its value unchanged - no rounding, no unit conversion, no
clamping at ingest. Two normalizers describing the same instant **merge** (steps from
one endpoint, resting HR from another, both landing on one day's row), so no writer can
drop another writer's field. Anything the normalizer does not recognise is by
definition not dropped either: it is still in `raw_payload`, which is the point of §2.
A payload that cannot be parsed at all goes to `quarantine` with a reason, and is
counted - never silently skipped.

### 4. Watermarks, so overlap is the normal case

`dataset_watermark (dataset, source, last_day, last_ts, updated_at)`, keyed by
`(dataset, source)`:

* `last_day` is the last calendar day that was fetched **completely**; a day only
  advances it when every request for that day succeeded, so a half-fetched day is simply
  fetched again - costing requests, never correctness;
* watermarks are per source, so a CSV backfill cannot claim to have moved the API's
  watermark forward;
* they only move forward (`Watermarks.advance` keeps the larger value), so re-importing
  an old window cannot make tomorrow's default `--since` go backwards.

`dataset_run` records what each import run did per dataset - status, chunks, requests,
rows inserted, rows duplicate, payloads duplicate, quarantined, bytes stored, duration,
resulting watermark, error - which is where "did that run finish?" and the duplicate
counts in `fm store stats` come from.

### 5. Migrations

Migrations are **numbered SQL files** in `fitbit_monitor/store/migrations/`
(`0001_baseline.sql`, `0002_import.sql`, `0003_storage_model.sql`), not Python strings:
the schema is the part other people have to reason about, and a reviewer should be able
to read the next version of their data in SQL.

* the runner (`schema.migrate_store`) applies pending files in order, **one transaction
  per migration**, and records the version in `fm_meta('schema_version')`;
* **forward-only**: a committed file is never edited; the next number is added instead;
* `fm db init` creates and brings up to date, `fm db migrate` applies pending files,
  `fm db status` reports version, objects and pragmas. Ordinary commands migrate on
  open, so a store written by an older fm upgrades itself before anything reads it;
* **unknown version is refused, not guessed**: a database recording a version newer than
  this build raises `StoreError` telling the user to upgrade fm. Writing into a schema
  you have not seen is the one failure mode that is unrecoverable.

### 6. Worked example: two overlapping imports of the same intraday day

Import A fetches 1–7 May. Import B, a week later, fetches 5–12 May. For 5 May both ask
for the same window - under the current source, `POST users/me/dataTypes/heart-rate/dataPoints:rollUp`
naming `2026-05-05T00:00:00Z .. 2026-05-06T00:00:00Z` at a 60-second window (under the
retired Fitbit API it was `GET /1/user/-/heart/date/2026-05-05/1d/1min.json`; the example
works identically either way, which is the point of keying on content):

1. A stores the body (hash `h`) and normalizes 1 440 minute rows.
2. B's response hashes to `h` again → `INSERT OR IGNORE` stores nothing and counts
   `payloads_duplicate = 1`. `raw_payload` still holds exactly one row for that day.
3. B normalizes the same 1 440 timestamps. Each is an upsert on
   `(dataset='heart_rate', ts_utc=…)`; `json_patch` merges each payload with itself, so
   the rows are unchanged and `rows_duplicate = 1440`.
4. If the source had *revised* 5 May (a value corrected later), the same 1 440 instants
   still hit the same keys: values update, row count does not.
5. The watermark for `(heart_rate, api)` moves to 12 May and never moves back.

The test assertion is the one the ticket asks for: after both runs, `SELECT ts_utc FROM
series WHERE dataset='heart_rate' GROUP BY ts_utc HAVING COUNT(*) > 1` is empty, and
exactly one raw payload covers that day.

### 7. Retention, integrity, and getting the data out

* **Default retention: nothing is deleted.** The store is the user's copy and the API
  only goes back so far, so the safe default is to keep everything. `fm store prune
  --days N` exists for people who want a bound, and **its default is a dry run**:
  nothing is deleted until `--execute` is passed. `--what` defaults to `raw` (the
  bulkiest, and the only part that can be re-fetched) and never touches watermarks.
  The documented default horizon for anyone who does prune is 365 days.
* `fm store verify` re-hashes every retained payload against its `content_hash`, checks
  claimed sizes, `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, the registry, and
  that every stored payload is valid JSON with a valid instant - and exits nonzero when
  any of it is not.
* `fm store export --format jsonl|csv` writes either the normalized series (wide CSV,
  one column per field) or the original payloads, bodies included.
* `fm store backup` uses SQLite's online **backup API**, not `cp`, so a snapshot taken
  while an import is running is a consistent snapshot of committed state; the copy is
  integrity-checked before the command reports success.
* **Encryption at rest: deliberately out of scope, for now.** SQLCipher is a C extension,
  which would break the stdlib-only rule that keeps a fresh clone runnable and a plugin
  author's setup free; it needs a key, and a key file next to the database it protects is
  theatre. The threat that actually applies to a personal machine - a lost laptop, or a
  cloud backup of the data directory - is handled by full-disk encryption (which the
  user already has or should enable), by writing the store `0600`, and by
  `docs/data-policy.md` naming the directories to exclude from sync/backup. Revisit if a
  threat model with shared machines or unencrypted sync arrives; see `docs/data-policy.md`.

### 8. Read surface, and what "typed" means

`ReadOnlyStore` is the only read API and the only thing a plugin receives:
range iterators over a dataset, `day()`, `coverage()`, `gaps()`/`completeness()`,
`stored_datasets()`, `describe()`/`units()`, and raw-payload access. Values are floats;
**units come from the dataset catalog in `store/datasets.py`**, one table for the whole
program, so a plugin's `MetricResult.unit` and `fm store stats` cannot disagree.
Downsampling names its aggregation (`downsample(points, "hour", how="max")`). The
convention for timestamps is stated once, in `docs/storage.md`: everything is UTC and
aware, windows are half-open, and a daily point sits at UTC midnight of its *source day*
- the date is the meaning, the clock time is a placeholder.

## Consequences

* One writer path, one dedup key, and an import that can be interrupted anywhere.
* Raw retention is affordable, so "re-derive it later" is a real option rather than a
  slogan; `fm store stats` reports the byte cost per dataset, so it stays honest.
* The typed surface is views plus a units catalog rather than a table per dataset: adding
  a dataset costs a normalizer and a view, not a schema migration.
* Every stored measurement is also a stored hash of itself, so corruption is detectable
  rather than invisible - and `verify` is the command that says so.
* The open question this ADR does not close is scale: a multi-year intraday history is
  millions of rows in one table. It is the right problem to have, the indexes that
  matter already exist, and §2 records the escape hatch if raw outgrows the file.
