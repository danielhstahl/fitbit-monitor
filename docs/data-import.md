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

> **Which API, and its status (2026-11-22, ported 2026-11-23).** The source is the
> **Google Health API** at `https://health.googleapis.com/v4`, with the export archive as
> an equal first-class source; the whole decision, its sources and its dates are
> [ADR-0002](adr/0002-health-api-source.md). The port in that ADR's section 10 has landed:
> `[api]` ships the v4 host, the four `googlehealth.*` scopes, `users/me/...` paths and
> the rollup verbs, so `fm auth login` and `fm import` talk to the live service. What did
> not change is everything below - same commands, same datasets, same report - which is
> the point the ADR made when it called this a port and not a build.
>
> The retired **Fitbit Web API** (`api.fitbit.com`, `/1/user/-/…`) is still refused rather
> than dialled, because a config left pointing at it produces the one error a person
> cannot distinguish from a bad login: it answers `401` forever. If your `[api]` table
> names it, `fm import`, `fm api probe` and `fm auth login` exit **6** with the date and
> the alternative, and `fm api source` says where the setting is:
>
> ```console
> $ fm api source
> configured source   Google Health API (health.googleapis.com)
>   base_url           https://health.googleapis.com/v4
>   authorize_url      https://accounts.google.com/o/oauth2/v2/auth
>   token_url          https://oauth2.googleapis.com/token
>   decided instead    Google Health API  https://health.googleapis.com/v4
>   decision           docs/adr/0002-health-api-source.md (section 10 is the port)
>   works today        `fm import --source csv --path ARCHIVE` (Google Takeout) needs no API at all
>
> Scope names go the same way, because an existing config can outlive the API it was
> written for. `fm config init` used to write Fitbit's own scope names - `activity`,
> `heart_profile`, `heartbeat`, `profile`, `sleep`, `spo2` - and none of them is a Google
> scope: consent comes back as `Error 400: invalid_scope / Request details:
> flowName=GeneralOAuthFlow`, in a browser tab, after the password, which is the least
> place in the system that can explain itself. Each retired name is therefore mapped to
> the `googlehealth.*` scope that now covers it - the five heart-related ones all land on
> `health_metrics_and_measurements.readonly` - and the rewrite is announced rather than
> silent:
>
> ```console
> $ fm auth login
> note: "activity" was a Fitbit scope; it is now activity_and_fitness.readonly
> note: "spo2" was a Fitbit scope; it is now health_metrics_and_measurements.readonly
> 1. open this URL in a browser and approve the request:
> ```
>
> A scope that maps to nothing - `nutrition`, `settings`, any `.writeonly`, or a string
> that is not a Google scope at all - stops the login in the terminal, naming the line:
> `fm config show` prints why, and `fm config init --force` rewrites the file. Both stay
> runnable on the broken file on purpose, because they are how you fix it.
> ```
>
> `fm api source --verify` asks the hosts what they are today, with no token and no client
> id, and flags anything that has drifted from what ADR-0002 measured on 2026-11-23 - a
> retired host that has started answering `410`, or a data endpoint that answers a
> stranger with `200`, are both reasons to re-read that ADR.

## What "imported" means

Five datasets, each with its own data types and aggregation shape (see
`src/fitbit_monitor/api/catalog.py`, the only place that knows Google Health's data types,
filter strings and response shapes):

| dataset | data types asked for | stored as | chunk |
|---|---|---|---|
| `heart_rate` | `heart-rate` rollup, 60-second windows | one point per minute: `hr`, `hr_min`, `hr_max` | 6 days (one rollup request each) |
| `daily_summary` | `steps`, `active-minutes`, `total-calories`, `floors` daily rollups, plus `daily-resting-heart-rate` | one point per civil day, merged from five responses | 14 days |
| `sleep` | `sleep` data points | one point per sleep log (naps included) | 7 days |
| `hrv` | `daily-heart-rate-variability` | one point per day, `rmssd`, `sdnn` | 14 days |
| `spo2` | `daily-oxygen-saturation` | one point per night, `spo2_avg/min/max` | 14 days |

The chunk is the page-safe window for that data type, not a courtesy: a heart-rate rollup
answers about six days of minute buckets before it wants a page token, and a daily rollup
answers a fortnight in one go, so those are the sizes the importer commits at.

Two rules apply to all of them:

* **Raw first.** The response body is stored verbatim in `raw_payload`, keyed by
  content hash, before anything is interpreted. A normalization bug is therefore a
  bug you can fix later by re-reading the store, not lost data.
* **Timestamps are UTC, and the account timezone says what the local strings
  meant.** The intraday rollups carry explicit `...T00:05:00Z` instants, but the daily
  rollups and the sleep logs describe *civil* date-times with no offset - "the day
  2026-05-01", "asleep from 23:40" - and `[import] timezone` is the one setting that
  resolves them (see [Configuration](#configuration)). Sleep prefers the physical
  `startTime` when the response carries one, so a trip changes what is stored without
  the setting having to change.

## Idempotency: run it again, nothing duplicates

Three mechanisms, all visible in the report:

| what | where | effect |
|---|---|---|
| content hash | `raw_payload.content_hash` | the same body is stored once; `payloads_duplicate` counts the rest |
| row upsert on `(dataset, ts_utc)` | `series` | re-importing a day merges values (`UPDATE ... SET payload = json_patch(...)`) instead of overwriting or duplicating; `rows_duplicate` counts them |
| forward-only watermark | `dataset_watermark` | the next import starts the day after the last one that fully succeeded, per dataset and per source |
| overlap window | `[import] overlap_days = 1` | the last day or two is re-read on the next run, because the service recalculates sleep and HRV after a late sync; a dataset already current through the end is left alone |

`fm import --since 2026-05-01 --until 2026-05-14` twice in a row is a supported
thing to do: the second run costs requests and adds no rows.

One writer per store: while an import is running, a second `fm import` against the
same database is refused ("already writing ... wait for it to finish") rather than
interleaving chunks or fighting over the rate-limit budget. The lock is an advisory
`flock` beside the file, so a crashed import never leaves the store locked.

## Planning before spending

The legacy API allowed ~150 requests per hour per user, and a full history was tens of
thousands of them, which is why the plan is a first-class artifact. This source is
different in kind: 300 requests per minute per user, requests that each cover a
*window* instead of a day, and no history limit at all - a five-year per-minute heart-rate
backfill is about 300 requests rather than about twelve hours of wall clock
(ADR-0002 §6). The plan stays mandatory either way, because the expensive question is
now "how many rows will this write" more than "how many calls will this cost".

```console
$ fm import --since 2026-05-01 --until 2026-05-14 --dry-run
planned import (api) 2026-05-01 .. 2026-05-14 (14 days)
  daily_summary   2026-05-01 .. 2026-05-14  1 chunk(s) of 14d, ~5 requests
  heart_rate      2026-05-01 .. 2026-05-14  3 chunk(s) of 6d, ~3 requests
  hrv             2026-05-01 .. 2026-05-14  1 chunk(s) of 14d, ~1 requests
  sleep           2026-05-01 .. 2026-05-14  2 chunk(s) of 7d, ~2 requests
  spo2            2026-05-01 .. 2026-05-14  1 chunk(s) of 14d, ~1 requests
  total requests (approx): 13
```

Thirteen requests for the same fourteen days that cost seventy-three against the old API.
A chunk is now as wide as the endpoint's page-safe window - six days of minute buckets,
fourteen days of daily rollups - so the count grows with the *window*, not with the day
count, and the chunk stays the unit of durability that ADR-0005 describes.

`--dry-run` does not touch the store (it works before a store exists). `--json`
gives the same plan as data, for scripts.

Before fetching, `fm import` asks `users/me/profile` once: the granted scopes and the
account's membership date decide which datasets are even possible and where
`--full-history` starts. A dataset you lack the scope for is a **skip with a reason**, not
a silent zero:

```console
  note: skipping 1 dataset(s) with no granted scope: daily_summary: token has no
        'activity_and_fitness.readonly' scope
```

## Resuming, interrupting, and bounding a run

A **chunk** is a few days of one dataset, and it is the unit of durability: its
payloads and rows commit together, and the watermark only advances over chunks that
fully succeeded. That makes every ugly stop cheap:

* **Ctrl-C (130).** The in-flight chunk is rolled back, everything committed stays.
  `fm import --resume` re-plans the *original* window from the last run and starts
  each dataset at its own watermark.
* **Rate limit.** This API publishes a per-minute quota and sends no `X-RateLimit-*`
  headers to pace against, so the client counts its own: `[api] requests_per_second`
  (5 by default) is a local token bucket, and a `429 RESOURCE_EXHAUSTED` is slept out up
  to `[import] max_wait_seconds`. A `Retry-After` longer than that aborts the run with a
  note to re-run, because burning a day's budget waiting is never what you meant. If a
  response ever *does* carry a ceiling, `[import] rate_limit_reserve` starts biting
  against it - the same code path, waiting for headers that have not arrived yet.
* **`--max-requests N`.** For cron: bound the spend, run it hourly, and each run
  picks up at the watermark.
* **`--retry`.** Re-imports only the datasets whose last attempt failed.

ETags are used automatically: a re-fetch of a day that has not changed costs a 304
and no payload.

## When a dataset fails

Failures are per dataset, and the run reports *partial* rather than pretending:

| symptom | report | exit |
|---|---|---|
| `[api]` names a host that was switched off (the retired Fitbit Web API) | refused before any request, with the date and the alternative | 6 |
| one dataset 403s (scope, or no SpO2 on the account) | that dataset `failed`, the rest `ok` | 3 |
| a request the API refuses - `400 INVALID_ARGUMENT` from a filter this build got wrong | that dataset `failed`, printed as `error:` and not `warning:` | 3 |
| a data type the token cannot read, inside a dataset that otherwise answered | `info` note naming `MISSING_OAUTH_SCOPE`, rows from the rest still land | 0 |
| a period with nothing in it | nothing: v4 answers an empty window with an empty page | 0 |
| payload is not JSON / a row is unusable | counted in `quarantined`, body kept | 5 |
| everything failed | `failed` | 1 |
| Ctrl-C | `interrupted`, completed chunks stored | 130 |

A failed dataset prints `error:`, not `warning:`, because the rows are not in the store and
a word people skim is how a whole dataset goes missing quietly. And the recovery is always
the same command: a watermark only moves over days that finished, so nothing that failed was
marked imported.

`fm import history`, `fm import watermarks` and `fm import report` answer "what did
that run do?" afterwards - the history lives in the `dataset_run` table, one row per
dataset per run (with its error), and each run also writes a JSON report to
`<data-dir>/imports/<run-id>.json` (the last `[import] report_keep` of them).
`fm import watermarks` joins in each dataset's last status, so "how far did it get"
and "how did it end" are one table.

## Asking the API instead of reading the logs

The most common failure is a quiet zero: the run finished, and nothing arrived. The
reason is almost always one of four things - a scope the token was never given, a
dataset this account does not have, a rate limit, or an endpoint answering something
unexpected - and all four are questions you can *ask* rather than infer:

```console
$ fm api probe --since 2026-05-01 --until 2026-05-14
account   a1b2c3d4-e5f6-7890-1234-567890abcdef, member since 2022-11-08
          fitbit-era id: 4412789
scopes    granted: health_metrics_and_measurements.readonly, sleep.readonly
          wanted:  health_metrics_and_measurements.readonly, activity_and_fitness.readonly, sleep.readonly, profile.readonly
          missing activity_and_fitness.readonly - disables: daily_summary
          missing profile.readonly - disables: no dataset in the catalog (profile.readonly is identity only)
datasets  2026-05-01 .. 2026-05-14
  [no_scope]    daily_summary  activity_and_fitness.readonly            token has no 'activity_and_fitness.readonly' scope; not asked for
  [data]        heart_rate     health_metrics_and_measurements.readonly data 2026-05-01 .. 2026-05-14; sampled back to 2026-05-01 (--since 2026-05-01), every sampled day had data
  [data]        hrv            health_metrics_and_measurements.readonly data 2026-05-01 .. 2026-05-14; sampled back to 2026-05-01 (--since 2026-05-01), every sampled day had data
  [data]        sleep          sleep.readonly                           data 2026-05-01 .. 2026-05-14; sampled back to 2026-05-01 (--since 2026-05-01), every sampled day had data
  [data]        spo2           health_metrics_and_measurements.readonly data 2026-05-01 .. 2026-05-14; sampled back to 2026-05-01 (--since 2026-05-01), every sampled day had data
rate limit no ceiling sent by the API (v4 sends no X-RateLimit-* headers); this client holds itself to 5 requests/second
requests  26
```

That is the whole point of the command: it says *this token was never given the activity
scope*, which is a `fm auth login` you have to re-run, instead of a run that finished with
exit 0 and an empty table.

Four answers, one call each, all of them about the account the stored token belongs to:

* **scopes** - granted versus wanted, as the short names (`sleep.readonly`) because the
  URLs are 70 characters apiece and differ only in the middle word. Every gap names the
  datasets it disables and whether `fm auth login` was even asked for it (a `[api] scopes`
  list shorter than the catalog's is a silent way to disable a dataset at login time).
  The four v4 scopes are wider than the legacy six were: one of them covers heart rate,
  HRV and SpO2 together, so a single missing grant can disable four datasets.
* **datasets** - `data` (and its dates), `no_data` (404: nothing for those days),
  `forbidden` (403), `unauthorized` (401), `rate_limited`, `server_error`, `unreachable`,
  `empty` (200 with nothing readable in it) or `unknown`, with the API's own words in the
  message. Nothing an endpoint answers with is turned into a crash, so one refused
  dataset still leaves the others reported.
* **history** - the earliest date of data per dataset, found by a doubling walk back from
  today (1, 2, 4, 8 ... days, then the floor) rather than one request per day - and a
  cheaper unit than the legacy one, since a day and a week of heart rate cost one rollup
  each. The row
  says how sure it is: `account_floor` (every sampled day back to the floor had data, so
  the data reaches *at least* that far), `bounded` (an older sample came back empty, so
  the data starts between that day and the earliest seen) or `budget` (the
  `--max-requests` cap stopped the search). `--no-history` skips the walk entirely: one
  request per dataset, and no claim about older data.
* **rate limit** - what the API said about its ceiling, verbatim. Which for this API is
  nothing: it publishes a per-minute quota in prose and no headers, so the line says so and
  reports the local ceiling in `[api] requests_per_second` instead. Headers are parsed and
  quoted exactly if a response ever carries them.

It is read-only, bounded by `--max-requests` (default 60 - a probe is a diagnostic, not a
backfill), and `--json` prints the same report as one object for a script or for
`fm doctor --probe`. The exit code follows `fm import`: **0** every dataset the catalog
knows answered with data, **5** some did not, **1** nothing did, **2** there was no token
to ask with, **6** the source itself is retired and nothing was asked. `fm api probe
--fixtures DIR` rehearses the whole thing offline.

## Backfilling from an export archive

Years of history arrive faster from an archive than from any API, and the archive needs
no client id, no consent and no network, so it is the first load *and* the way back in
when the API is unavailable (ADR-0002 §8). Today the archive is **Google Takeout**;
Fitbit's old `fitbit.com/export-your-data` page is gone (a `404` as of 2026-11-22).

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
base_url = "https://health.googleapis.com/v4"    # the default; a host under
                                                 # api.fitbit.com is refused (exit 6)
token_url = "https://oauth2.googleapis.com/token"
client_id = "..."        # your own OAuth client, Personal mode (no secret is shipped)
redirect_port = 55413    # must match the registered redirect URI exactly
timeout = 30.0
retries = 3
requests_per_second = 5.0

[import]
timezone = "Europe/Berlin"            # what the un-offset civil timestamps mean
recent_days = 7                       # default window when there is no watermark
overlap_days = 1                      # re-read this many imported days for late corrections
rate_limit_reserve = 5                # only bites if the API ever sends a ceiling
max_wait_seconds = 90                 # do not sleep longer than this for a limit
confirm_above_requests = 2000         # ask before a pull this big
report_keep = 20                      # JSON reports kept in <data-dir>/imports/
[import.chunk_days]
heart_rate = 1                        # commit a day at a time instead of the 6-day window:
                                      # finer resume, six times the requests
```

`fm config show` prints the effective configuration; `fm config init` writes a
commented starting point.

## Practising without an account

`--fixtures DIR` (or `FM_FIXTURES`) replays recorded responses from a directory
instead of calling the network - the same mechanism the test suite uses
(`tests/fixtures/api`, written by `scripts/dev/record_fixtures.py`). It is useful
for rehearsing an expensive import, and it means the import tests, and this
document's examples, run offline. Replay is offline by definition, so the
retired-source refusal does not apply to it, and the corpus is asked for in exactly the
requests a real import makes:

```console
$ fm import --fixtures tests/fixtures/api --since 2026-05-01 --until 2026-05-14
daily_summary  2026-05-01 .. 2026-05-14  (1 chunk(s))
  [1/1] 2026-05-01..2026-05-14: +14 new, =0 already there
  => ok: 14 new rows, 56 already there
heart_rate     2026-05-01 .. 2026-05-14  (3 chunk(s))
  [1/3] 2026-05-01..2026-05-06: +1440 new
  [2/3] 2026-05-07..2026-05-12: +1728 new
  [3/3] 2026-05-13..2026-05-14: +576 new
  => ok: 3744 new rows, 0 already there
hrv            2026-05-01 .. 2026-05-14  (1 chunk(s))
  [1/1] 2026-05-01..2026-05-14: +14 new
  => ok: 14 new rows, 0 already there
sleep          2026-05-01 .. 2026-05-14  (2 chunk(s))
  [1/2] 2026-05-01..2026-05-07: +8 new
  [2/2] 2026-05-08..2026-05-14: +8 new
  => ok: 16 new rows, 0 already there
spo2           2026-05-01 .. 2026-05-14  (1 chunk(s))
  [1/1] 2026-05-01..2026-05-14: +13 new
  => ok: 13 new rows, 0 already there
DATASET        STATUS  CHUNKS  NEW   EXISTING  QUAR.  REQS  WATERMARK
daily_summary  ok      1/1     14    56        0      5     2026-05-14
heart_rate     ok      3/3     3744  0         0      3     2026-05-14
hrv            ok      1/1     14    0         0      1     2026-05-14
sleep          ok      2/2     16    0         0      2     2026-05-14
spo2           ok      1/1     13    0         0      1     2026-05-14

run import-...: ok - 3801 new rows, 56 already present, 12 requests, 0 quarantined, 55 ms
```

The corpus is generated, not scraped: `scripts/dev/record_fixtures.py` contains a
documented stand-in for the analyzer (see its module docstring for what it copies and
what it invents), so the fixtures are a *contract test* of the shapes this build expects
rather than evidence that the live service answers that way. Verifying them against a real
Personal-mode project is ADR-0002 §10 step 1, and it has not been done. `fm auth login --manual` covers the headless-machine case; the
fixture transport is picked up from `FM_FIXTURES`.

## See also

* [ADR-0002](adr/0002-health-api-source.md) - the source itself: which API, the four
  scopes, why a personal account can get access without an approval queue, the limits,
  and the archive as a first-class source.
* [ADR-0005](adr/0005-ad-hoc-import.md) - why chunks, watermarks and per-dataset
  failure are the shape of this thing.
* [data-policy.md](data-policy.md) - what leaves the machine, now that the API is
  reachable.
