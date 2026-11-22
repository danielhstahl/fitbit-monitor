# The local store

Everything `fm` knows lives in one SQLite file, and that file is the point of the
program: your data, on your machine, re-derivable. This is the reference for what is in
it, how it is versioned, and how to read it - either from the command line or through the
read API that plugins and reports use.

* The decisions and the reasons: [ADR-0003](adr/0003-storage-model.md).
* What leaves the machine and what does not: [data policy](data-policy.md).
* Getting data in: [data import](data-import.md).

```
  API / CSV export
        |
        v
  raw_payload ............ the response bytes, gzip, content-addressed
        |                    \
        |                     \  re-normalize later (that is the point of keeping raw)
        v                      v
  series ................. one row per (dataset, instant)
        |
        v
  ReadOnlyStore .......... range iterators, coverage, gaps, downsample, raw access
        |
        v
  plugins, reports, `fm store`
```

## Where the file is

`$XDG_DATA_HOME/fitbit-monitor/store.sqlite3` (default `~/.local/share/fitbit-monitor/`),
overridable per command with `--db`, `--data-dir`, `FM_DB` or `[store] path` in
`config.toml`. Every open uses WAL journal mode, foreign keys `ON` and a 5-second busy
timeout, and every writer takes a single-writer lock next to the file.

## Commands

| command | what it does |
|---|---|
| `fm db init` | create the store and apply every migration (idempotent) |
| `fm db migrate` | apply pending migrations to an existing store |
| `fm db status [--json]` | schema version, tables/views/indexes, pragmas in force |
| `fm store stats [--json]` | per-dataset row counts, date coverage, raw bytes vs normalized bytes, duplicate and quarantine counts |
| `fm store verify [--quick] [--sample N] [--json]` | re-hash every retained payload and re-check the invariants; **nonzero exit on trouble** |
| `fm store export --format jsonl\|csv [--what series\|raw] [--dataset D] [--since D] [--until D] [--output F]` | portable copy of the series or of the original payloads |
| `fm store backup [--output F]` | one consistent snapshot file, via SQLite's online backup API |
| `fm store prune --days N [--what raw\|series\|quarantine\|all] [--execute] [--vacuum]` | retention - **a dry run unless you pass `--execute`** |
| `fm dev seed --days 90 --seed 7` | deterministic synthetic corpus (see below - not real data) |

Ordinary commands migrate the store on open, so `fm db migrate` is for inspection and
for the deliberate upgrade path; `fm db status` is how you check before an upgrade
instead of after one.

```
fm db status
fm store stats
fm store verify
fm store export --format csv --dataset heart_rate --output hr.csv
fm store backup --output /backups/store.sqlite3
fm store prune --days 365
```

### What is in the database

| table | holds |
|---|---|
| `series` | the normalized record: `(dataset, ts_utc, payload JSON, source_id)` |
| `raw_payload` | the response bytes: content hash (unique), dataset, source, requested range, fetched_at, sizes, gzip blob |
| `dataset` | registry of datasets the store has seen, with kind and native grain |
| `dataset_watermark` | last completely-fetched day per dataset **per source** |
| `dataset_run` | what each import run did, per dataset |
| `quarantine` | payloads that could not be used, with the reason |
| `alerts` | alert history (dedup key, severity, evidence, occurrences) |
| `dev_annotation` | labels for injected synthetic episodes - never written by real data |
| `v_heart_rate_intraday`, `v_daily_summary`, `v_sleep`, `v_hrv` | typed views over `series`, with units in the column names |

Two identities matter:

* **`(dataset, ts_utc)` is the dedup key.** Re-importing a window lands on the same rows
  and patches the JSON payload instead of adding a copy - which is why overlapping
  imports, resumes and CSV-plus-API both end up with exactly one row per instant.
* **`content_hash` is the raw dedup key.** Two imports that fetched the same bytes store
  one payload. Nothing is stored twice because two commands asked for it.

### Timestamps: UTC, always, and what a "daily" point means

* `ts_utc` is ISO-8601 text with an explicit `+00:00`. fm refuses naive datetimes at
  every boundary rather than guessing one.
* Query windows are **half-open**: `[since, until)`.
* An **intraday** point is a real instant: the source's naive local timestamp, read in
  `[import] timezone`, converted to UTC.
* A **daily** point (`daily_summary`, `hrv`, sleep totals) is stored at
  `YYYY-MM-DDT00:00:00+00:00` for its *source day*: the **date is the meaning and the
  clock time is a placeholder**. That is how a local-midnight boundary is represented -
  as the day, not as an instant in some zone. Read `point.ts.date()`; do not shift the
  value into a local timezone to "recover" the date, and do not subtract 24 hours to get
  "yesterday" across a DST boundary - subtract a day from the date.

## The read API

One class, `ReadOnlyStore`, opened `mode=ro` with `query_only` on. It is what
`ctx.store` is inside a plugin, and there is no write path on it; a plugin that reaches
for `store.conn` to write is refused by SQLite.

```python
from datetime import UTC, datetime, timedelta
from fitbit_monitor.store import ReadOnlyStore

with ReadOnlyStore("store.sqlite3") as store:
    since = datetime.now(UTC) - timedelta(days=30)

    print(store.stored_datasets()[0].as_dict())      # what is in here, and its units
    print(store.units("heart_rate"))                 # {'hr': 'beats per minute'}
    print(store.describe("hrv").kind)                # 'daily'

    for point in store.query("heart_rate", since):   # half-open [since, now)
        print(point.ts, point["hr"], point.unit("hr"))

    hourly = store.query("heart_rate", since)
    from fitbit_monitor.store import downsample
    peaks = downsample(hourly, "hour", how="max")    # the method is in the signature

    day = store.day("daily_summary", "2026-05-31")   # one day, one row
    report = store.completeness("heart_rate", since, datetime.now(UTC))
    if not report.complete:
        print("gaps:", [g.as_dict() for g in report.gaps])
    raw = store.raw_for("heart_rate", datetime.now(UTC))   # the original response
```

| accessor | gives |
|---|---|
| `datasets()`, `stored_datasets()` | dataset names; then kind, grain, points, first/last, days present, raw bytes |
| `query(dataset, since, until, fields=…, order=…, limit=…)`, `series(dataset, window)` | `SeriesPoint` iterators over a half-open window |
| `day(dataset, day)` | one calendar day as a list of points |
| `first_ts`, `last_ts`, `latest`, `count` | the cheap edges of a series |
| `coverage(dataset, since, until)` | points, days expected/present, `missing_days` |
| `gaps(dataset, since, until)`, `completeness(...)` | breaks longer than ~3× the series' own grain - flagged, never silently skipped |
| `describe(dataset)`, `units(dataset)`, `grain_seconds(dataset)` | shape and units from the catalog (`store/datasets.py`) |
| `iter_raw(...)`, `raw_payload(hash)`, `raw_for(dataset, ts)`, `raw_totals()` | the retained responses, decompressed |
| `downsample(points, period, how=…)`, `aggregate(values, how)`, `daily_values(points, field)` | per-minute/hour/day/week/month buckets with the aggregation named |

Completeness is explicit because a quiet average across a missing day is a number that
never happened. A gap threshold defaults to three times the coarser of the catalog grain
and the median spacing actually present, so a weekly HRV series is not "all gaps" and a
heart-rate series that stopped for a day is - and `max_gap_seconds` overrides it.

`fm store export` is this read path written out: JSONL keeps the nested shape, CSV is
wide with one column per field, and a field a row does not carry is an empty cell, so
"not measured" stays distinguishable from zero.

## Integrity, backup, and retention

`fm store verify` answers "is this file what it claims to be?" by re-deriving the
answer from the bytes:

| check | what it proves |
|---|---|
| `schema_version` | the file is from a version of fm that exists |
| `integrity_check` | SQLite's own structure check |
| `foreign_keys` | no payload points at a dataset that is not there |
| `dataset_registry` | nothing is stored under a dataset fm never registered |
| `series_rows` | every stored payload is valid JSON with a valid instant |
| `raw_payload_hashes` | each retained body still hashes to its `content_hash`, and its claimed sizes are right |

Exit code is nonzero when any check fails, so it is usable from cron or a CI job.
`--quick` skips the (slow, honest) body re-hash; `--sample 1000` bounds it.

`fm store backup` takes a snapshot through SQLite's online backup API while the store is
being written to, then integrity-checks the copy. That is the supported way to copy a
live store; `cp` of a WAL database can produce a file that only looks like a database.

**Retention: nothing is deleted by default**, and `fm store prune` will not delete
anything until you pass `--execute`:

```
fm store prune --days 365              # prints exactly what would go, changes nothing
fm store prune --days 365 --execute    # deletes raw payloads fetched more than a year ago
fm store prune --days 365 --what all --execute --vacuum
```

`--what` defaults to `raw`: the retained responses are the bulk of the file and the only
part the source may still be able to re-send. Pruning never touches watermarks, so a
pruned day is still a day fm knows it fetched. If you want the filesystem space back,
`--vacuum` rewrites the file (or take a fresh `fm store backup` and keep that).

## Deterministic sample data

`fm dev seed` writes a synthetic corpus - 90 days of intraday heart rate plus daily
summaries, HRV, sleep and SpO2 by default - with overnight dips, exercise bursts, a few
missing days, and **labelled** abnormal episodes (`sustained_high`, `sustained_low`,
`irregular`) recorded in `dev_annotation`. Same `--seed` and `--end`, same bytes: the
benchmark baseline and the plugin conformance fixtures depend on that.

**It is synthetic data about nobody.** It did not come from an account or a device, it
never leaves the machine, and the episodes are labelled as synthetic so a detector test
cannot mistake one for a real arrhythmia.

```
fm dev seed --days 90 --seed 7
fm dev seed --days 30 --no-anomalies
fm dev seed --days 14 --anomalies 2 --fresh --json
fm dev info
```
