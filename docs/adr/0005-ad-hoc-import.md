# ADR-0005: the ad-hoc import - planning, chunks, watermarks, and failure

- **Status:** accepted
- **Date:** 2026-11-22
- **Implements:** `fitbit_monitor.api` (client, auth, catalog, http), `fitbit_monitor.ingest` (importer, pipeline, csv_source, report, runlog), `fitbit_monitor.store.watermarks`, `fm auth` / `fm import`
- **Related:** ADR-0003 (storage model), ADR-0004 (plugin contract - a plugin must never be able to break an import), [docs/data-import.md](../data-import.md)

## Context

The request is one line - "pull a chosen date range on demand" - and the reason it is
an epic rather than a `for` loop is everything around it:

* The API is **rate-limited** (~150 requests/hour) and a full history at 1-minute
  heart-rate granularity is tens of thousands of requests. A command that cannot
  tell you its cost before spending it is a command you are afraid to run.
* A pull is **long-running and interruptible**. It will be Ctrl-C'd, killed by a
  laptop closing, cut off by a rate limit, or left half-done at 3am by cron.
* Datasets are **not uniform**. HRV caps its window at 7 days; daily summary is two
  endpoints merged; intraday heart rate is one request per day; sleep returns naps;
  SpO2 does not exist on every account, and returns 404 for nights without a watch.
* Some days genuinely have **no data** (404), which is not an error, while some
  errors (403 on a scope you never granted) are permanent and must not be retried.
* Whatever is fetched must land in a store that plugins already read, without
  duplicating anything when a range is asked for twice.

Plus one requirement that outlives the epic: an import must be **inspectable after
the fact**, because "it imported" is not an answer anyone can act on.

## Decision

### 1. Plan, then fetch - and say what it will cost

`ImportPlanner` turns the CLI's `--since/--until/--dataset/--full-history` into an
`ImportPlan`: per dataset, the days, the chunks, and a request estimate. `--dry-run`
prints it and stops; `--json` emits it for scripts. The planner is also where the
`/1/user/-/profile.json` probe lives: granted scopes plus account resources decide
what is importable, and everything excluded is excluded **with a printed reason**.
A failure of the probe itself (rate limit, network) is not fatal - selection falls
back to the token's scopes.

### 2. The chunk is the unit of durability

A chunk is a few days of one dataset (per dataset, from the catalog; HRV 7 days,
most others 1). Its raw payloads and derived rows commit together, and the
watermark advances only over chunks in which *every* request succeeded. Rows are
idempotent (see 3), so ambiguity costs requests and never correctness.

That single rule is what makes Ctrl-C, rate limits, `--max-requests` and `--resume`
all the same simple story: what is stored is stored, and the next run re-fetches
exactly the ambiguous part. `--resume` re-plans the window of the previous run
(recorded in `dataset_run`) rather than silently narrowing to the default recent
window - a half-finished year-long backfill continues as a year.

### 3. Idempotency at three levels

| level | mechanism | visible as |
|---|---|---|
| payload | `raw_payload` keyed by content hash (`INSERT OR IGNORE`) | `payloads_duplicate` |
| row | `series` upsert on `(dataset, ts_utc)`, `json_patch` merge | `rows_duplicate` |
| range | `dataset_watermark` per (dataset, source), forward-only | `watermark` in the report, `fm import watermarks` |

The `json_patch` merge is deliberate: `daily_summary` writes the same instant twice
(two endpoints), and a second writer must add fields, not erase them.

Raw is stored **before** normalization, so a normalizer bug is repairable from the
store instead of requiring a re-fetch of a year.

### 4. Failure is per dataset, and the exit code says which kind

A dataset that 403s is marked `failed` with its reason while the others continue.
A day with no data is an `info` note, not a failure. unusable payloads/rows are
`quarantined` - counted, named, and still on disk. Exit codes are a contract,
printed in `fm import --help` so a scheduler can key on them:

| code | meaning |
|---|---|
| 0 | everything asked for landed |
| 1 | nothing landed |
| 3 | partial - some datasets landed, some did not |
| 5 | it landed, with warnings (quarantined rows) |
| 130 | interrupted; completed chunks are stored |

(2 is argparse/usage; 4 is "no store yet", shared with the rest of `fm`.)

Rate limiting is handled **before** the wall: the client tracks
`X-RateLimit-Remaining` against `[import] rate_limit_reserve` and raises `StopRun`
so the run stops itself rather than discovering the limit via 429. A short
`Retry-After` is slept out (bounded by `max_wait_seconds`); a reset hours away ends
the run with instructions. ETags turn an unchanged refetch into a 304.

### 5. One ingest path for every source

`--source api` and `--source csv` (web export / Takeout) share `Ingest`: same
dedupe, same quarantine, same report, same watermarks (tagged `source=csv`). The CSV
half matches files by archive path and, failing that, by header signature, and
names every file it declines to read. That keeps "backfill years from the export,
then keep it current from the API" a two-command story instead of two codebases.

### 6. Everything is written down

Each run writes an `ImportReport` - one `DatasetReport` per dataset with range,
chunks, rows, duplicates, quarantine, requests, bytes and watermark - to
`<data-dir>/imports/<run-id>.json` (last `report_keep` kept) and to the `dataset_run`
table (one row per dataset per run). `fm import report|history|watermarks` read
them back; `--retry` and `--resume` are driven by the same rows.

### 7. No network in the tests

`ReplayTransport` serves recorded responses from `tests/fixtures/api`, keyed by
`"GET /path?query"`, selected by `--fixtures DIR`/`FM_FIXTURES`. The fixtures were
recorded once by `scripts/dev/record_fixtures.py` against a real account (redacted
names, dates in 2026) and include the awkward cases on purpose: a 404 day for heart
rate and SpO2, two naps on one day, a 7-day HRV window. The headline test - a
two-week import landing exactly the expected rows, twice - runs offline.

## Consequences

* The catalog is the single place endpoint knowledge lives; adding a dataset is a
  catalog entry (requests + normalizer) plus a fixture, not a new code path.
* Chunk size is a trade-off you can dial in config: bigger chunks are fewer
  transactions and coarser resume; `--chunk-days` overrides per run.
* Watermarks are per (dataset, source) and forward-only, which means a *deliberate*
  backfill of an old window (`--since`) does the refetch you asked for rather than
  being optimised away - idempotency makes that safe, just not free.
* `dataset_run.since/until` are nullable, because a report row for a dataset that
  never started is legitimate.
* Tests hold the contract: `tests/test_importer.py` (plan + engine + failure paths),
  `tests/test_import_cli.py` (exit codes and the CLI surface),
  `tests/test_ingest.py` (dedupe/quarantine/watermarks/report),
  `tests/test_catalog.py` (endpoint shapes and normalizers).
