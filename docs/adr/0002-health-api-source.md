# ADR-0002: the health data source - the Google Health API, a Takeout archive, and one adapter

- **Status:** accepted
- **Date:** 2026-11-22
- **Implements:** *forward-looking* - `fitbit_monitor/api/` (client, auth, catalog, http),
  `[api]` in `config.py`, the `--source` switch in `cli/import_cmd.py`,
  `fitbit_monitor/ingest/csv_source.py`. **As of this ADR the code still speaks to the
  legacy Fitbit Web API** (`https://api.fitbit.com`, `/1/user/-/…`, `X-RateLimit-*`),
  which returns no data (§2). §10 is the ordered list of what has to change and where.
- **Related:** ADR-0001 (zero runtime dependencies - it constrains every choice here),
  ADR-0003 (raw retention - the reason a source swap is not a data-loss event),
  ADR-0004 (plugins never learn which source produced a row), ADR-0005 (chunks and
  watermarks - the shape this source has to fit), [docs/data-import.md](../data-import.md)
  (user-facing), [README.md](../../README.md)

## Context

The original request said "google health api" and the user's device is a Fitbit. The
ticket read those as two different vendors and asked which one to build against. In
2026 they are the same company and, after three years of consolidation, nearly the same
API - but not the one this repository's code targets. The ticket also asked the right
practical question: *can a person with one Fitbit and one Google account actually get
programmatic access today, and if not, does the export archive become the primary
route?* Both questions now have dated, checkable answers, and one of them arrived by
surprise: the API this code was written against no longer returns data.

### 1. What happened, in dates

| date | event | source (all retrieved 2026-11-22) |
|---|---|---|
| 2021-01-15 | Google completes its acquisition of Fitbit | historical; not re-verified against a source here |
| 2024-05-01 | **Google Fit**: new developer sign-ups closed | `developers.google.com/fit` |
| 2026 (year) | **Google Fit APIs deprecated**, REST reference removed from the site | `developers.google.com/fit`, `/fit/rest/v1/overview` → HTTP 404 |
| 2026-09-30 | **Fitbit Web API: end of support** (no bug fixes, no API support) | `developers.google.com/health/about` |
| 2026-10-01 | Google Health API `v4beta` public beta channel opened | `/health/release-notes` |
| 2026-11-02 | **Fitbit Web API and Fitbit OAuth endpoints turned off**; requests return `410 Gone` | `/health/about` |

The two vendor pages do not agree on the kill date, which is worth recording because it
is the kind of thing that makes a sunset date unfalsifiable in practice. The legacy
site's banner says:

> "Support for the legacy Fitbit Web API ends on September 30, 2026. After this date the
> API will continue to operate, but Google will no longer fix bugs or provide API
> support. On **October 30, 2026**, the Fitbit Web API will be turned off and will no
> longer function or sync data to or from Fitbit users." — `dev.fitbit.com` banner

and the current developer site says:

> "**November 2, 2026 (API turn-off):** The Fitbit Web API and Fitbit OAuth endpoints
> were turned off and no longer function or sync data to or from Google Health users.
> Requests to these endpoints fail and return an HTTP `410 Gone` status code."
> — `developers.google.com/health/about`

This repository's date is 2026-11-22, i.e. **eleven days after the later of the two
dates**. The discrepancy does not matter to the decision: on both readings the legacy
API is gone. What does matter is what we measured from this machine (see *How to
re-verify*): the legacy host is *not* returning `410`, it is returning a plausible
Fitbit-flavoured `401 invalid_token`, which is the worst possible failure mode for an
import tool - it looks like "you need to log in again" rather than "this API is dead".
A client that can only get 401s will tell the user to run `fm auth login` forever.

### 2. Every candidate, measured today

| candidate | verdict | measured 2026-11-22 |
|---|---|---|
| **Fitbit Web API** (`api.fitbit.com`) | **dead.** Rejected as a source. | `GET /1/user/-/profile.json` → `401 {"errors":[{"errorType":"invalid_token", …}]}`; docs promise `410 Gone` |
| **Fitbit OAuth** (`accounts.fitbit.com/oauth2/authorize`) | not usable: Google's own turn-off notice names the Fitbit OAuth endpoints alongside the API; the authorize page still renders, which is worse than a `410` | `200` (HTML) |
| **Google Fit REST API** (`fitness.googleapis.com`) | **dead soon and closed now.** Rejected. | "The Google Fit APIs, including the Google Fit REST API, will be deprecated in 2026. As of May 1, 2024, developers cannot sign up to use these APIs." ; `/fit/rest/v1/overview` → `404` |
| **Google Health API** (`health.googleapis.com`) | **the one.** This is the Fitbit Web API's successor, on Google OAuth, serving Fitbit + Pixel Watch data | `GET /v4/users/me/dataTypes/heart-rate/dataPoints` → `401` (alive, wants a Google token) |
| **Takeout / export archive** | **first-class second source**, needs no credentials at all | `fitbit.com/export-your-data` → `404`; Takeout lives, and its product list contains `googlehealth` |
| **Health Connect** | not a *remote* source: an on-device Android store. Out of scope for a macOS CLI | - |

The two rows that end in `401` look identical from a terminal and mean opposite things.
That asymmetry - and the fact that our current client would print
`token expired, run fm auth login` for the first one - is the single most important
thing this ADR records.

### 3. What the existing code assumed

`fitbit_monitor/api/` is a good client for a dead service: `client.py` budgets against
`X-RateLimit-Remaining`/`-Reset`, `auth.py` drives Fitbit's `accounts.fitbit.com`
authorize/token endpoints with PKCE, `catalog.py` names five `/1/user/-/…` endpoints,
and 60-odd fixtures under `tests/fixtures/api` replay those responses. None of it can
be nudged onto the new API: the paths, the response shapes, the aggregation model, the
auth endpoints and the rate-limit mechanism all change. The replay harness is what
makes that survivable (ADR-0005 §6): the fixtures are re-recordable and the tests do
not need the network. The store is not affected at all, because ADR-0003 §2 made the
raw payload the record and `series` a normalized view of it: a source swap costs
re-normalization, never data.

## Decision

| question the ticket asked | decision |
|---|---|
| (a) which API | **Google Health API, REST, stable `v4` channel, `https://health.googleapis.com/v4`.** Not the Fitbit Web API (off since 2026-11-02), not Google Fit REST (deprecated 2026, closed to new sign-ups since 2024-05-01), not "an adapter over both" - the legacy side of that adapter is a 410. |
| (b) can a person get access | **Yes, today, in about fifteen minutes, with no approval and no lead time**: a Google Cloud project in **Personal mode** (single user, no OAuth verification, no user cap, no 7-day token expiry). §5. |
| (c) scopes | **Four**, all `googlehealth.*.readonly` (§4). Everything in this tool's scope set collapses from six Fitbit names to four Google ones. |
| (d) limits | 300 requests/minute/user, 120 000/minute/project; page cap 10 000 (25 for `sleep`/`exercise`); rollup range cap 14 or 90 days. A five-year per-minute heart-rate backfill is **~300 requests over a minute of requests, not 1 826 requests over twelve hours** (§6). |
| (e) fallback | The **Takeout archive is a first-class source, not a fallback**, and it is the CI/dev source (§8). The API is *also* able to backfill history now, so the old premise - "the archive is the only way to get years" - is retired. |
| (f) adapter interface | **Required.** `Source` (§9): one protocol, three implementations (`health`, `archive`, `fixture`), and a rule that a new source touches neither the store nor the plugins. |

### 4. Scopes: four strings, and what is refused without each

All Google Health scopes share one prefix and all of them are Restricted:

> "All Google Health API scopes begin with: `https://www.googleapis.com/auth/googlehealth`"
> "All Google Health API scopes are categorized as Restricted, necessitating a privacy
> and security review for access." — `/health/scopes`, `/health/about`

The ticket asked for `heartrate`, `intraday`/`heart-rate`, `activity`, `sleep`, `Spo2`,
`profile`. The legacy six-to-four collapse is the answer, and it is why "intraday"
disappears as a concept: the new API serves intraday data on the same endpoints as
everything else (§6).

| decided scope (append to the prefix) | grants, of what this tool uses | internal datasets that need it |
|---|---|---|
| `…googlehealth.health_metrics_and_measurements.readonly` | `heart-rate` (1-second storage), `daily-resting-heart-rate`, `heart-rate-variability`, `daily-heart-rate-variability`, `oxygen-saturation`, `daily-oxygen-saturation`, `daily-respiratory-rate`, weight, glucose | `heart_rate`, `hrv`, `spo2`, and the resting-HR half of `daily_summary` |
| `…googlehealth.activity_and_fitness.readonly` | `steps`, `distance`, `floors`, `active-minutes`, `active-zone-minutes`, `total-calories`, `active-energy-burned`, `time-in-heart-rate-zone`, `vo2-max` | `daily_summary` (steps/calories/minutes half) |
| `…googlehealth.sleep.readonly` | `sleep` sessions, stages, awakenings | `sleep` |
| `…googlehealth.profile.readonly` | `users/getProfile` (age, member-since), plus identity | none stored; used as the probe |

`https://www.googleapis.com/auth/googlehealth.profile.readonly` also appears in the
`users/getIdentity` scope list, and so does every other read scope - identity works on
any grant, so the probe must not *require* profile.

**Deliberately not requested**, with the reason, so nobody re-adds them in a hurry:

- `.ecg.readonly`, `.irn.readonly` (irregular rhythm notifications) - clinically
  interesting and directly relevant to the arrhythmia epic (fitb-cel), but a scope is a
  promise about data you actually read. Add them when a plugin consumes them, not
  before. The IRN data type is named in this ADR so that ticket can find it.
- every `.writeonly` scope - this tool never writes health data (ADR-0003's ingest is
  one-directional), and Google's scope guidance says "Only configure 'write' access …
  if you are sending data to Google Health".
- `.nutrition.readonly`, `.location.readonly`, `.reproductive_health.writeonly`,
  `.logged_symptoms.writeonly`, `.mindfulness.writeonly`, `.settings.readonly` - no
  consumer in this repository.

**What is refused without them** (from the v4 error catalog):

| missing | API says | our behaviour today |
|---|---|---|
| a data type's read scope | `403` `MISSING_OAUTH_SCOPE` ("Required OAuth scope(s) are missing for this operation") | dataset marked `failed` with the reason; run exits `3` |
| a scope not configured on the consent screen | `403` `DISALLOWED_OAUTH_SCOPES` | usage error naming the console page |
| write-only grant on a read path | `403` `PERMISSION_DENIED` | n/a (no writes) |
| everything declined at consent | no health scopes on the token | `fm auth login` reports zero consent and exits `2` |

That last row is not a courtesy: Google's own scope guidance *requires* graceful
degradation - "Never crash, show a generic technical error, or render a blank screen if
a user withholds one or more requested scopes" - and names the pattern
*partial consent*. `ImportPlanner` already resolves datasets from the granted scopes
and skips the rest with a reason (`catalog.permitted_datasets`,
`docs/data-import.md`), so the policy is already implemented; only its inputs change:
granted scopes come from the OAuth token response, and the account probe becomes
`users/getProfile` instead of `/1/user/-/profile.json`.

### 5. Whether a person can get this, and what it costs

Three paths exist; the first is the one this tool documents.

| path | verification | user cap | refresh-token lifetime | fit |
|---|---|---|---|---|
| **Personal mode** (recommended) | none | none (1 account: the creator) | not shortened | yes - this is a single-user local tool |
| External, publishing status **Testing** | none to run, warning screen | 100 test users, each added by hand | **7 days** | works, but re-consent weekly |
| External, **In production** | OAuth app verification **and** CASA assessment | unbounded | normal | no - cost and lead time are absurd for one user |

Personal mode, quoted:

> "Personal mode apps do not require OAuth app verification, are not subject to the
> OAuth user cap or 7-day test user token expiration, and do not display the unverified
> app screen." — `support.google.com/cloud/answer/15549945`
> "If you are the only user of your app and you create your app using the Personal mode
> onboarding page, your app is restricted exclusively to your Google Account, does not
> display the unverified app screen, and does not need to go through verification."
> — `support.google.com/cloud/answer/7454865`

Three consequences of choosing it, all of them documented in `docs/data-import.md`
rather than hidden in code:

1. **It is a one-way door.** "once a project is configured with a user type of
   `Personal`, it cannot be changed to `External` or `Internal`". The README tells the
   user that before it tells them to click anything.
2. **One account, by construction.** A second Google account gets
   `client_user_mismatch`. `fm auth status` prints the identity it is bound to
   (`users/getIdentity` → `healthUserId`, `legacyUserId`), so the failure is legible.
3. **No approval lead time at all**, versus the alternative that the ticket feared.
   For completeness, the rejected path's real cost: restricted-scope verification means
   an OAuth app review plus a third-party CASA security assessment, and Google quotes
   "2-3 weeks for tier-2 applications and 4-6 weeks for tier-3 … fees … ranging from
   $500 to $4,500 USD", with annual re-verification on top. No personal tool should pay
   that, and none has to.

The setup steps the docs will carry (Google's own, in their order): create a Personal
mode project → enable *Google Health API* (`health.googleapis.com`) → create an OAuth
client → add exactly the four scopes on the **Data Access** page → note that
"The Google Health API doesn't provide a separate sandbox or staging environment. All of
your environments call the production Google Health API.", which is why `--fixtures`
(ADR-0005 §6) is the dev environment. Loopback redirects are
explicitly allowed for a local tool - "For testing, you can specify URIs that refer to
the local machine, such as `http://localhost:8080`" - so the PKCE loopback flow already
built in fitb-l8b.1 stays, and `fm auth login --manual` stays as the headless escape.

**The one assumption in this ADR that no document settles:** whether a Personal-mode
project may enable `health.googleapis.com` at all. Nothing in the Google Health docs
forbids it, nothing confirms it either. §10 task 1 is "run the setup once and find out",
and the fallback if the answer is no is the Testing row above - a weekly re-consent,
ugly and survivable - which is exactly why the archive source (§8) is first-class and
not a consolation prize.

### 6. Limits, page sizes, and what a backfill actually costs

Quotas, as published:

| metric | default limit | consequence for this tool |
|---|---|---|
| per user, per minute | **300 requests** (~5/s) | the client needs its own token bucket; the API documents **no** `X-RateLimit-*` headers, so `client.py`'s header-reading budget has nothing to read |
| per project, per minute | 120 000 | irrelevant to one user |
| per project, per day | 86.4 M | irrelevant to one user |
| OAuth token grants | 10 000 / 24 h | irrelevant to one user |
| unverified External app | 250 QPS total, capped at 100 users | avoided by Personal mode |
| page size | **10 000** for most data types, **25** for `sleep` and `exercise` | chunk sizing per dataset |
| `rollUp` / `dailyRollUp` range | **14 days** for `heart-rate`, `active-minutes`, `total-calories`, `calories-in-heart-rate-zone`; **90 days** for the rest | the new chunk sizes |
| `windowSize` | ≥ 1 s, and "equal to or larger than the underlying storage resolution" | 60 s rollups are the documented way to ask for per-minute |

Storage resolutions that matter: `heart-rate` **1 second** (it was 1 minute on the old
API), `steps`/`oxygen-saturation`/`active-minutes` 1 minute, HRV/SpO2/resting-HR
delivered as daily data types as well as raw.

**History is no longer the problem it was.** The docs are unambiguous:

> "You can query a user's data as far back as it has been recorded; the API imposes no
> limitations or restrictions on the amount of historical data your application can
> consume." — `/health/endpoints` (Query historical data)

Worked against our own five-year case (and these are the numbers ADR-0005 and
`docs/data-import.md` should carry once §10 lands):

| plan | requests | wall clock at 5 req/s |
|---|---|---|
| legacy, per-day intraday (`/heart/…/1d/1min.json`), 5 yr | 1 826 at ~150/h | **~12 hours** |
| **decided**: `heart-rate` `rollUp`, `windowSize=60s`, 6-day windows (8 640 points ≤ the 10 000 page cap → 1 page) | ~305 | **~1 minute** |
| same with the permitted maximum 14-day window (20 160 points → 3 pages) | ~393 | ~1.5 minutes |
| raw 1-second `list` (`--resolution raw`), 5 yr | ~16 000 pages | ~55 minutes, and ~158 M rows |

Two numbers to keep in the code, because they are the traps:

* a 14-day 60-second heart-rate rollup is 20 160 points, i.e. **three pages**; a 6-day
  window is 8 640, i.e. **one page**. Chunk size and page size must be chosen together,
  and the default plan should be the one-page window.
* per-second raw data is 60× the rows of per-minute. `series` at 1 Hz for five years is
  ~158 M rows, which is a different database than the one ADR-0003 sized. So: **default
  to 60-second rollups**, and expose raw sampling as an explicit, window-bounded
  option for the arrhythmia work (fitb-cel) rather than as the default import.

Other decisions forced by this section:

* **`reconcile`/`rollUp` over `list` where the shape allows.** Devices re-bucket
  telemetry on sync and `list` returns every overlapping record; the aggregation
  endpoints "automatically reconcile overlapping data points". Our dedupe is by content
  hash and `(dataset, ts)`, so overlaps cost bytes, not correctness - but rollups are
  both smaller and already reconciled. `list` stays available where a dataset has no
  rollup.
* **`fm import --dry-run` estimates stay mandatory** (ADR-0005). The estimate's units
  change from "requests at 150/hour" to "requests at 300/minute, page-aware".
* **Backoff on `429 RESOURCE_EXHAUSTED`, `503 UNAVAILABLE`, `504 DEADLINE_EXCEEDED`,
  with jitter** - the documented trio. `client.py` already retries 429/5xx with backoff;
  it gains a local bucket and loses its dependency on rate-limit headers.

### 7. The dataset map - the table Epic 2 and Epic 4 code from

Internal dataset names do not change: `heart_rate`, `daily_summary`, `sleep`, `hrv`,
`spo2`. They are ours (`series.dataset`, seed corpus, plugin `datasets` tuples, the
views in ADR-0003 §3) and the vendor's names are not leaking into any of it today.

| dataset | data type(s) | endpoint | scope | stored shape (unchanged) |
|---|---|---|---|---|
| `heart_rate` | `heart-rate` | `POST …/dataTypes/heart-rate/dataPoints:rollUp`, `windowSize="60s"`, ≤6-day windows | `health_metrics_and_measurements` | one row/minute: `hr` (min/max/avg of the window → `hr`; keep `hr_min`/`hr_max` too, they are free) |
| `daily_summary` | `daily-resting-heart-rate` + `steps`,`active-minutes`,`total-calories`,`floors` | `dataPoints:dailyRollUp` (`windowSizeDays=1`, ≤90-day windows) | both of the above | one row/day, fields merged from both scopes (ADR-0003 §3 merge) |
| `sleep` | `sleep` | `GET …/dataTypes/sleep/dataPoints` filtered on `sleep.interval.civil_end_time`, page size 25 | `sleep` | one row/session (naps included) |
| `hrv` | `daily-heart-rate-variability` (+ `heart-rate-variability` for per-night samples, later) | `GET …/dataTypes/daily-heart-rate-variability/dataPoints` | `health_metrics_and_measurements` | one row/day: `rmssd`, `sdnn`, `hrv_n` |
| `spo2` | `daily-oxygen-saturation` | `GET …/dataTypes/daily-oxygen-saturation/dataPoints` | `health_metrics_and_measurements` | one row/night: `spo2_avg`, `spo2_min`, `spo2_max` |
| *(probe)* | - | `GET …/v4/users/me/identity`, `GET …/users/me/profile` | any health scope / `profile` | replaces `/1/user/-/profile.json`; identity is what `fm auth status` prints |

Rules that come with the map:

* **Requests are windows now, not days.** A chunk is "one page of one window of one
  data type". `chunk_days` in `catalog.py` becomes `chunk_days` + `page_size`, with
  `heart_rate` at 6 (one page of 60-second rollups, §6) and the daily rollups at 30.
* **Civil time vs physical time.** Filters may not mix them
  (`INVALID_DATA_POINT_FILTER_MIXED_TIME_RESTRICTIONS`). Store stays UTC
  (ADR-0003); `[import] timezone` keeps meaning "what the account's civil dates meant",
  and it maps to the API's `civil_*` fields, which is more honest than the old
  "timestamps carry no offset" guess.
* **`ACCOUNT_NOT_LINKED` (400)** is a real, first-run answer: "The Google account is not
  linked to a Fitbit account." It gets its own message pointing at the Fitbit app
  link step, not a generic API error.
* **`v4` only.** `v4beta` is "not recommended" for production and may change without
  notice; a beta endpoint in the default path would make our fixtures a lottery.
  `skin-temperature-sensors` is beta-only today, so it is out.
* **Webhooks: not used.** They require a registered HTTPS subscriber endpoint, and this
  tool has no server and no public port; polling on `fm import` from cron is the design
  (ADR-0005). Recorded here so the next person does not "improve" it.

### 8. The archive is a first-class source, and it is the dev/CI source

Two separate questions, two different answers.

*(i) Is the export path the primary import route?* **It is primary for the case where
the API is not available at all, and it is no longer the only way to get history** (§6).
The practical ladder is:

1. **Archive first for the initial multi-year backfill**, because it costs zero
   requests, works before there is a client id, and lands years in one command.
2. **API for everything after that day** - watermark-driven, a handful of requests.
3. **Archive again whenever the API cannot be used**: no Google Cloud project, a
   Workspace-admin policy that blocks third-party apps, a `client_user_mismatch`, or a
   Personal-mode experiment that did not pan out.

What the archive *cannot* provide, so that nobody is surprised later: per-second heart
rate (its CSVs are per-minute), HRV samples below daily, anything whose scope was never
granted (irrelevant), and above all **recency** - Takeout is a snapshot: "Your data file
may not include changes made to your data between when you request a download and when
you create your archive." That sentence is the whole reason the API exists.

*(ii) Is it the CI/dev source?* **Yes, and it already is.** Three tiers, in the order a
contributor meets them:

| tier | what | needs |
|---|---|---|
| fixtures | `tests/fixtures/api` + `--fixtures` replay (ADR-0005 §6) | nothing - offline, deterministic, no secrets |
| archive | `tests/fixtures/export/takeout-sample` (the redacted sample from the spike fitb-l8b.7) and a real Takeout ZIP for a local soak run | a downloaded ZIP, no credentials |
| seed | `fm dev seed` synthetic corpus | nothing |

CI never calls the API: no network, no client secret on a runner, no quota spent on a
pull request. Recorded here because "should CI hit the real API?" is exactly the sort of
question an ADR is supposed to kill.

*(iii) Which export, now that the Fitbit page is a 404?* **Google Takeout**
(`takeout.google.com`), whose product list contains `googlehealth`; the old
`fitbit.com/export-your-data` flow is gone, measured today. Column-level facts -
filename patterns, units, the timezone each file means, and the files we deliberately
decline - are owned by the spike fitb-l8b.7 and live in `ingest/csv_source.py`
(`PARSERS`, `DECLINED`, `COLUMN_SIGNATURES`) and [docs/data-import.md](../data-import.md).
One open item, called out for Epic 4: confirm against a *current* Takeout archive that
the heart-rate CSV is still per-minute and that `Daily_Summary.csv` still spells its
columns the way the alias table expects, because that format is not a published
contract and it has changed before.

### 9. The `Source` interface is required

Required - and mostly already paid for. The store, the watermarks and the plugins do not
know what produced a row: `raw_payload.source` is a free-text column (`api`, `csv`, a
fixture directory), watermarks are keyed `(dataset, source)`, `Ingest` takes
`source=`, and a plugin sees only `series`. That is the whole reason a source swap is
possible at all, and it is the invariant to protect.

What is missing is a name. Today the choice is an `if` in `cli/import_cmd.py`
(`--source`, `choices=("api", "csv")`, with `--path` implying `csv`). The decision is to
make the seam explicit and to bound where a third source may live:

```python
class Source(Protocol):
    name: str            # "health" | "archive" | "fixture"
    def plan(self, window: Window, datasets: Sequence[str]) -> Plan: ...
    def payloads(self, plan: Plan) -> Iterator[Payload]: ...   # bytes + provenance
```

Three rules, testable and therefore binding:

1. **Adding a source touches `ingest/`, `cli/import_cmd.py` and a registry - not
   `store/`, not `plugins/`, not `ingest/pipeline.py`.** A source that needs a schema
   change or a plugin-side special case is a misfiled feature.
2. **`--source` names come from the registry**, so `--source health`, `--source archive`
   and any future `--source connect-export` validate in one place. `--source api` stays
   as a permanent alias of `health` so old crontabs and docs keep working, and the
   legacy Fitbit value is refused with a message that names the sunset rather than a
   401 ("the Fitbit Web API was turned off on 2026-11-02; see docs/adr/0002").
3. **Provenance stays visible.** Every payload carries the source id (the request URL or
   the archive member), and `fm import report` says which source a dataset's rows came
   from. A year from now, "did this number come from Takeout or from the API" must be
   answerable from the store.

### 10. What has to change, in order

Epic 2 and Epic 4 should need no further source decisions; this list is the whole
remaining surface.

1. **Verify Personal mode against `health.googleapis.com` once, by hand** (the only
   open assumption, §5). Record the answer in this file. Cost: one afternoon.
2. `config.py`: `[api] base_url` default → `https://health.googleapis.com/v4`; token
   endpoint → `https://oauth2.googleapis.com/token`; scopes default list → the four
   strings from §4; **refuse** a `base_url` under `api.fitbit.com` with the sunset
   message instead of dialling it.
3. `api/auth.py`: Google authorize/token endpoints, `prompt=consent` only when scopes
   change (documented pattern), loopback redirect unchanged, `client_user_mismatch` and
   the Testing-mode 7-day expiry mapped to distinct messages.
4. `api/http.py` + `api/client.py`: local 5 req/s token bucket, `Retry-After`, jittered
   backoff on `429/503/504`, `Retry` semantics per AIP error catalog; delete the
   `X-RateLimit-*` budget or keep it as an *additional* signal.
5. `api/catalog.py`: rewrite as the §7 map - data types, endpoint builders
   (`list`/`rollUp`/`dailyRollUp`, filter strings in snake_case, path names in
   kebab-case), normalizers for the new response shapes, chunk sizes = page-safe
   windows.
6. `cli/import_cmd.py`: `--source health|archive` with `api`/`csv` aliases (same flag,
   same exit codes - ADR-0001's contract is unchanged), `--resolution minute|raw`
   bounded to a window (§6).
7. Re-record `tests/fixtures/api` against `v4` with the existing recording transport,
   and re-run `scripts/dev/record_fixtures.py --check`; the replay harness is why steps
   2-6 are testable without a Google account.
8. Docs: `docs/data-import.md` (setup ladder, the new budget arithmetic, Takeout steps),
   `README.md` (one line: which API and why), and this file's §10 tick-marks.
9. **Do not** add `ecg`/`irn` scopes, webhooks, `v4beta`, or per-second default sampling
   here. Each is a decision of its own, and three of the four belong to fitb-cel.

## Consequences

* The premise of the ticket - "two APIs, pick one, maybe the CSV is the escape hatch" -
  is replaced by one live API, one dead pair, and an archive that is a source in its own
  right. Epic 2's remaining work is a port, not a build, and the fixtures make it safe.
* **Everything already shipped against the legacy API is now a liability with a date on
  it.** The user docs asserted a live Fitbit API - "Fitbit allows ~150 requests per hour
  per user" in `docs/data-import.md`, and a `fm import` path in the README - so this
  change corrects both to say what is dead, what replaces it, and where the port plan
  lives, rather than letting them quietly lie.
* The rate-limit story inverts: the new per-user budget (300/min) makes a full-history
  import a coffee break instead of an overnight job, which is why `--dry-run`'s job
  changes from "warn before spending a day" to "warn before writing 158 M rows".
* Health data still leaves the machine to exactly one host (`health.googleapis.com`,
  configured overridable but single) - `docs/data-policy.md`'s promise needs a name in
  it, and gets one.
* Personal mode makes the tool's distribution story honest: it is a tool **you**
  configure for **your** account, not a hosted service with a verified client. That is
  consistent with everything else in this ADR set, and it is why there is no client
  secret in the repository and never will be.
* The archive fixtures from fitb-l8b.7 are load-bearing, not decorative: they are the
  only body of real-world export data we own, and Epic 4's acceptance depends on one
  fresh Takeout archive being added next to them.

## Rejected

* **Keep building on the Fitbit Web API.** It answers `401` and will not answer
  anything else; its OAuth endpoints no longer issue tokens. Rejected on the physics of
  a dead service, not on merit.
* **Google Fit REST API.** Deprecated in 2026 and closed to new developers since
  2024-05-01; its REST reference page is already a 404. Even before that it was a
  worse fit: it is the Android/Google-Fit store, not where Fitbit device data lives
  now, and it has no path to the Fitbit account.
* **An adapter that supports both Fitbit Web and Google Health from day one.** The
  migration guide recommends exactly this while both are live; both are not live, so the
  legacy half would be dead code with a live-looking failure mode (`401`, "run
  `fm auth login`"). The adapter survives - `Source` (§9) - its second live
  implementation does not.
* **Personal mode is too clever / just ask for verification.** The verification route
  is $500-$4,500 and 2-6 weeks per year, for one user, on a tool whose data never
  leaves the machine except to Google. Rejected on proportionality.
* **Default to per-second heart rate.** 60× the rows for a signal no plugin in this
  repository uses; ADR-0003's storage model is sized for minutes. Opt-in instead (§6).
* **Webhooks / a push model.** Needs a public HTTPS subscriber; this is a laptop CLI.
  Polling from cron stays, with watermark-based resume making it cheap.
* **Recording fixtures from the live API in CI.** Secrets and quota on a shared runner
  for a suite that must be offline anyway (ADR-0005 §6).
* **Renaming the internal datasets to the vendor's data-type names.** The names in
  `series` are used by the seed corpus, the typed views and third-party plugins; the
  §7 map is where the vendor's vocabulary stops.

## Sources and dates

Every factual claim above, with its quote, its home, and the date it was read. All
retrieved **2026-11-22** from this repository's build environment, which has network
egress (verified with the probe below).

| # | claim | quoted text | URL |
|---|---|---|---|
| 1 | legacy Web API end of support 2026-09-30, turn-off 2026-11-02, `410 Gone` | "November 2, 2026 (API turn-off): … turned off and no longer function … return an HTTP `410 Gone` status code." | `developers.google.com/health/about` |
| 2 | the older 2026-10-30 date | "On October 30, 2026, the Fitbit Web API will be turned off…" | `dev.fitbit.com` (banner, all pages) |
| 3 | Google Health is the successor, all scopes Restricted | "The Google Health API is the next generation of the Fitbit Web API."; "All Google Health API scopes are categorized as Restricted…" | `developers.google.com/health`, `/health/about` |
| 4 | base URIs `health.googleapis.com/v4`, `v4beta` not for production | "REST base URI `https://health.googleapis.com/v4`"; "Using `v4beta` endpoints or beta data types in production applications is not recommended." | `/health/about` |
| 5 | scope names and prefix | table of 18 scopes under `https://www.googleapis.com/auth/googlehealth` | `/health/scopes` |
| 6 | scope → data type map (heart rate/HRV/SpO2 under `health_metrics_and_measurements`, sleep under `sleep`, activity under `activity_and_fitness`) | scope-map table | `/health/filters#required-oauth-scopes` |
| 7 | 403 without the scope | "`MISSING_OAUTH_SCOPE` \| `403` \| Required OAuth scope(s) are missing for this operation."; `DISALLOWED_OAUTH_SCOPES` | `/health/reference/rest/v4/errors` |
| 8 | quotas | "Per user (minutely) 300 requests / minute"; "Per project (minutely) 120 000"; unverified "Max 250 QPS total (capped at 100 users @ 2.5 QPS per user)" | `/health/rate-limits` |
| 9 | 429/503/504 with exponential backoff and jitter | "implement retry logic with exponential backoff and random jitter on `429 RESOURCE_EXHAUSTED`, `503 UNAVAILABLE`, and `504 DEADLINE_EXCEEDED`" | `/health/rate-limits` |
| 10 | page caps 10 000 / 25 | "for certain data types like `exercise` and `sleep`, the default and maximum page size is capped at 25" | `/health/endpoints` |
| 11 | rollup range caps 14 / 90 days; windowSize ≥ resolution | "A maximum range of 14 days for `heart-rate`…; a maximum range of 90 days for all other rollup data types."; "choose a `windowSize` that is equal to or larger than the underlying storage resolution (`60s` for 1-minute steps)" | `/health/endpoints`, `/health/aggregate-data` |
| 12 | unlimited history | "You can query a user's data as far back as it has been recorded; the API imposes no limitations…" | `/health/endpoints` |
| 13 | heart-rate storage resolution 1 s; rollups reconcile | "**Storage resolution:** 1 second"; rollups "group and summarize … over a reconciled data stream" | `/health/data-types`, `/health/aggregate-data` |
| 14 | `getIdentity` gives `healthUserId`/`legacyUserId`; `ACCOUNT_NOT_LINKED` = "The Google account is not linked to a Fitbit account." | reference pages | `/health/reference/rest/v4/users/getIdentity`, `/health/reference/rest/v4/errors` |
| 15 | Personal mode: no verification, no cap, no 7-day expiry | "Personal mode apps do not require OAuth app verification, are not subject to the OAuth user cap or 7-day test user token expiration, and do not display the unverified app screen." | `support.google.com/cloud/answer/15549945`, `/answer/7454865` |
| 16 | Personal mode is irreversible and single-account | "once a project is configured with a user type of `Personal`, it cannot be changed…"; `client_user_mismatch` | `support.google.com/cloud/answer/15549945` |
| 17 | Testing status expires authorizations in 7 days | "Authorizations by a test user will expire seven days from the time of consent." | same |
| 18 | restricted-scope verification cost and lead time | "2-3 weeks for tier-2 applications and 4-6 weeks for tier-3 … $500 to $4,500 USD"; "Apps requesting restricted scopes data need to complete 're-verification' annually." | `/health/app-verification`, `support.google.com/cloud/answer/13463073` |
| 19 | loopback redirect allowed | "For testing, you can specify URIs that refer to the local machine, such as `http://localhost:8080`." | `developers.google.com/identity/protocols/oauth2/web-server` |
| 20 | no sandbox environment | "The Google Health API doesn't provide a separate sandbox or staging environment." | `/health/setup` |
| 21 | Google Fit APIs deprecated in 2026, sign-ups closed 2024-05-01 | "The Google Fit APIs, including the Google Fit REST API, will be deprecated in 2026. As of May 1, 2024, developers cannot sign up to use these APIs." | `developers.google.com/fit` |
| 22 | Google Fit REST reference is gone | `GET https://developers.google.com/fit/rest/v1/overview` → `404` | measured |
| 23 | Takeout is the export route, and the Fitbit export page is gone | "Go to the Google Takeout page…"; takeout product list contains `googlehealth`; `fitbit.com/export-your-data` → `404` | `support.google.com/fit/answer/3024190`, `takeout.google.com`, measured |
| 24 | Takeout is a snapshot, not a stream | "Your data file may not include changes made to your data between when you request a download and when you create your archive." | `support.google.com/fit/answer/3024190` |
| 25 | legacy host answers `401`, not `410`, today | `{"errors":[{"errorType":"invalid_token","message":"Access token invalid: x…"}` | measured |
| 26 | export CSV column/unit inventory | `PARSERS`, `DECLINED`, `COLUMN_SIGNATURES`, `tests/fixtures/export/takeout-sample` | this repo, spike fitb-l8b.7 (closed 2026-11-22) |

Claims 1-21 and 24 are vendor text, quoted verbatim where quoted; 22, 23 and 25 are
measurements taken from this environment on the date above; 26 is our own inventory and
the only one of them that is *not* a vendor promise - the export CSV layout has changed
before and is not documented as a contract, so treat it as an observation with an expiry
date, not a specification.

## How to re-verify, and when this ADR expires

A decision about someone else's API has a shelf life, so it ships with a way to check
it. Run this and compare against §2; it takes ten seconds and needs no credentials:

```console
$ date -u -Iminutes; for u in \
    https://api.fitbit.com/1/user/-/profile.json \
    https://health.googleapis.com/v4/users/me/dataTypes/heart-rate/dataPoints \
    https://developers.google.com/health/about; do printf '%s  ' "$(curl -sS -o /dev/null -w '%{http_code}' -m 20 "$u")"; echo "$u"; done
```

Expected on the day this was written: `401` (legacy, but treat as dead), `401`
(alive, wants a Google token), `200`.

Re-open this ADR when any of these is true - each is a dated, observable event, not a
mood:

* `health.googleapis.com` returns a non-4xx status without a token, or a documented
  quota number changes (§6).
* A Google Health data type we depend on moves to `v4beta`, is deprecated, or is
  removed - the release notes are the place to watch, and `skin-temperature-sensors`
  shows the pattern.
* Google changes Personal mode (a user-type rule, or the restricted-scope policy under
  it). §5's assumption gets revisited either way.
* The Takeout health archive changes layout again, which is §8's open item and which
  `csv_source.py`'s alias tables will surface as skipped-with-reason files rather than
  silently.
* A second source becomes real (Health Connect export, another wearable). Then §9's
  registry is the place it goes, and this ADR gains an amendment rather than a
  successor.
