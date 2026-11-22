# Getting your data in: `fm auth` and `fm import`

Everything the store knows about your body arrives through two commands. `fm auth`
gets a token; `fm import` moves data. This document is the long version of
`fm import --help`: what gets asked for, what lands, what happens when it goes
wrong, and what it costs.

```bash
fm auth login                       # browser consent, token stored 0600
fm import --dry-run                 # what would this fetch, and at what cost
fm import                           # fetch it
fm import report                    # what actually happened
```

The token file is written `0600` and **refused at read time** if anything widened it
(`chmod 600 <path>` is the fix offered): a refresh token readable by the machine is
the whole account. `fm config path` prints every resolved location if a store or a
token file is not where you expected.

## What "imported" means

Five datasets, each with its own endpoint shape (see
`src/fitbit_monitor/api/catalog.py`, the only place that knows Fitbit's URLs):

| dataset | endpoint | stored as | chunk |
|---|---|---|---|
| `heart_rate` | `/heart/{date}/{duration}` intraday | one point per minute, `hr` | 1 day |
| `daily_summary` | `/activities/{date}` + `/activities/heart/{date}/1d` | one point per day, merged | 1 day |
| `sleep` | `/sleep/{date}` | one point per sleep log (naps included) | 1 day |
| `hrv` | `/hrv/{date}/{N}d` | one point per day, `rmssd`, `sdnn` | 7 days (endpoint maximum) |
| `spo2` | `/spo2/{date}/1d` | one point per night, `spo2_avg/min/max` | 1 day |

Two rules apply to all of them:

* **Raw first.** The response body is stored verbatim in `raw_payload`, keyed by
  content hash, before anything is interpreted. A normalization bug is therefore a
  bug you can fix later by re-reading the store, not lost data.
* **Timestamps are UTC, and the account timezone says what the local strings
  meant.** Fitbit's `dateTime` values carry no offset; `[import] timezone` is the
  one setting that resolves them (see [Configuration](#configuration)).

## Idempotency: run it again, nothing duplicates

Three mechanisms, all visible in the report:

| what | where | effect |
|---|---|---|
| content hash | `raw_payload.content_hash` | the same body is stored once; `payloads_duplicate` counts the rest |
| row upsert on `(dataset, ts_utc)` | `series` | re-importing a day merges values (`UPDATE ... SET payload = json_patch(...)`) instead of overwriting or duplicating; `rows_duplicate` counts them |
| forward-only watermark | `dataset_watermark` | the next import starts the day after the last one that fully succeeded, per dataset and per source |
| overlap window | `[import] overlap_days = 1` | the last day or two is re-read on the next run, because Fitbit recalculates sleep and HRV after a late sync; a dataset already current through the end is left alone |

`fm import --since 2026-05-01 --until 2026-05-14` twice in a row is a supported
thing to do: the second run costs requests and adds no rows.

One writer per store: while an import is running, a second `fm import` against the
same database is refused ("already writing ... wait for it to finish") rather than
interleaving chunks or fighting over the rate-limit budget. The lock is an advisory
`flock` beside the file, so a crashed import never leaves the store locked.

## Planning before spending

Fitbit allows ~150 requests per hour per user, and a full history is tens of
thousands of them. So the plan is a first-class artifact:

```console
$ fm import --since 2026-05-01 --until 2026-05-14 --dry-run
planned import (api) 2026-05-01 .. 2026-05-14 (14 days)
  daily_summary   2026-05-01 .. 2026-05-14  14 chunk(s) of 1d, ~28 requests
  heart_rate      2026-05-01 .. 2026-05-14  14 chunk(s) of 1d, ~14 requests
  hrv             2026-05-01 .. 2026-05-14  2 chunk(s) of 7d, ~2 requests
  sleep           2026-05-01 .. 2026-05-14  14 chunk(s) of 1d, ~14 requests
  spo2            2026-05-01 .. 2026-05-14  14 chunk(s) of 1d, ~14 requests
  total requests (approx): 73
```

`--dry-run` does not touch the store (it works before a store exists). `--json`
gives the same plan as data, for scripts.

Before fetching, `fm import` also asks `/1/user/-/profile.json` once: the granted
scopes and the account's available resources decide which datasets are even
possible. A dataset you lack the scope for is a **skip with a reason**, not a
silent zero:

```console
  note: not imported: spo2 - scope "spo2" is not in the token's granted scopes
```

## Resuming, interrupting, and bounding a run

A **chunk** is a few days of one dataset, and it is the unit of durability: its
payloads and rows commit together, and the watermark only advances over chunks that
fully succeeded. That makes every ugly stop cheap:

* **Ctrl-C (130).** The in-flight chunk is rolled back, everything committed stays.
  `fm import --resume` re-plans the *original* window from the last run and starts
  each dataset at its own watermark.
* **Rate limit.** The client watches `X-RateLimit-Remaining` against
  `[import] rate_limit_reserve` and stops the run *before* exhausting the bucket
  rather than finding out via 429. A short `Retry-After` is slept out; a reset hours
  away aborts the run with a note to re-run, because burning a day's budget waiting
  is never what you meant.
* **`--max-requests N`.** For cron: bound the spend, run it hourly, and each run
  picks up at the watermark.
* **`--retry`.** Re-imports only the datasets whose last attempt failed.

ETags are used automatically: a re-fetch of a day that has not changed costs a 304
and no payload.

## When a dataset fails

Failures are per dataset, and the run reports *partial* rather than pretending:

| symptom | report | exit |
|---|---|---|
| one dataset 403s (scope, or no SpO2 on the account) | that dataset `failed`, the rest `ok` | 3 |
| a day genuinely has no data (404) | `info` note on the dataset, still `ok` | 0 |
| payload is not JSON / a row is unusable | counted in `quarantined`, body kept | 5 |
| everything failed | `failed` | 1 |
| Ctrl-C | `interrupted`, completed chunks stored | 130 |

`fm import history`, `fm import watermarks` and `fm import report` answer "what did
that run do?" afterwards - the history lives in the `dataset_run` table, one row per
dataset per run (with its error), and each run also writes a JSON report to
`<data-dir>/imports/<run-id>.json` (the last `[import] report_keep` of them).
`fm import watermarks` joins in each dataset's last status, so "how far did it get"
and "how did it end" are one table.

## Backfilling from a CSV export

The API is capped at ~1 request per hour for a full history at 1-minute
granularity, so the web export (or Google Takeout) is often the faster first load:

```bash
fm import --source csv --path ~/Downloads/export.zip --dry-run   # what is in there
fm import --source csv --path ~/Downloads/export.zip
```

Files are matched by path (`hrv/HRV*.csv`, `sleep/Sleep*.csv`,
`activities/HeartRate*.csv`, `activities/Daily_Summary.csv`) and, when a path says
nothing, by header (`Minutes Asleep`, `Hrv Value`, `HeartRate`, `Steps`). Same
ingest path, same dedupe, same report - the watermark just records `source=csv`.
Files that look plausible but are deliberately *not* imported say so in the notes
(the intraday activity log, body weight) rather than being quietly dropped.

`--file`/`--glob`/`--exclude` narrow a large archive; `--since/--until` narrow by
date as usual.

## Configuration

```toml
[api]
base_url = "https://api.fitbit.com"   # the only host data is sent to
client_id = "..."
redirect_port = 55432
timeout = 30.0
retries = 3

[import]
timezone = "Europe/Berlin"            # what Fitbit's un-offset timestamps mean
recent_days = 7                       # default window when there is no watermark
overlap_days = 1                      # re-read this many imported days for late corrections
rate_limit_reserve = 5                # stop with fewer than this many left
max_wait_seconds = 90                 # do not sleep longer than this for a limit
confirm_above_requests = 2000         # ask before a pull this big
report_keep = 20                      # JSON reports kept in <data-dir>/imports/
[import.chunk_days]
heart_rate = 1                        # finer resume, more requests
```

`fm config show` prints the effective configuration; `fm config init` writes a
commented starting point.

## Practising without Fitbit

`--fixtures DIR` (or `FM_FIXTURES`) replays recorded responses from a directory
instead of calling the network - the same mechanism the test suite uses
(`tests/fixtures/api`, recorded by `scripts/dev/record_fixtures.py`). It is useful
for rehearsing an expensive import, and it means the import tests, and this
document's examples, run offline. `fm auth login --fixtures` and `--manual` cover
the headless-machine case.

## See also

* [ADR-0005](adr/0005-ad-hoc-import.md) - why chunks, watermarks and per-dataset
  failure are the shape of this thing.
* [data-policy.md](data-policy.md) - what leaves the machine, now that the API is
  reachable.
