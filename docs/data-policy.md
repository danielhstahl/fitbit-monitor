# Data policy: what leaves this machine, and what never does

fitbit-monitor is local-first. This document is the short, checkable statement of
the data policy - the one that the webhook code, the plugin contract and the tests
are written to agree with.

## The rule

**Your health data stays on this machine.** The program talks to exactly two
destinations, both because you asked: the health API - the **Google Health API** at
`health.googleapis.com`, decided in
[ADR-0002](adr/0002-health-api-source.md), whose OAuth endpoints are Google's - when and
only when you run `fm auth` or `fm import`, and the alert webhook you configure. There
is no third party, no background service, and no telemetry.

## In

| direction | what | where it goes |
|---|---|---|
| ingest | your Fitbit data (API import or a CSV export) | the local SQLite store |
| read | plugins, `fm` commands, the read API | local only |

### What the API call sends

Nothing you did not choose, and only while an import is running:

| request | sent | why |
|---|---|---|
| `fm auth login` / token refresh | your PKCE verifier and authorization code, then the refresh token | to get and renew a token, at `accounts.google.com` and `oauth2.googleapis.com` - the token plane moved with the data plane, so there is no legacy OAuth endpoint left to dial |
| `fm import` | `Authorization: Bearer <token>` and the window you asked for - in the query string for a list, in a small JSON body for a rollup - plus an `If-None-Match` ETag when we have one for a `GET` | to fetch the window you named, from `[api] base_url`, which ships as `https://health.googleapis.com/v4`. Since 2026-11-23 nothing at all is sent to the retired Fitbit host: a `[api]` table naming it is refused with exit 6 before the first request, so "the dead API" is not a destination this program has. Rollup `POST`s are never conditional, because an ETag would be claiming a window's contents are immutable |

Plain HTTP is refused for the webhook - `https` or the loopback exception, never a
plaintext internet hop. The API endpoints default to `https` and only change if you
edit `[api]` yourself (a loopback URL there is how the login flow is tested). No
query, metric or stored value is ever sent *out* to the health service; the calls are
reads.

Nothing else in the program opens a socket. Not the store, not the plugin runner,
not the alert history, not the benchmarks. An import is the only thing that
initiates contact with the health service, and `--fixtures DIR` runs the whole path with no
network at all.

## Out

| path | enabled by | what is sent | gate |
|---|---|---|---|
| `alerts.webhook.url` | you setting a URL and `enabled = true` | one JSON alert object: plugin, key, title, message, severity, timestamp, confidence, evidence, dedup key | `https://` required |

That is the complete list. There is no telemetry, no update check, no crash reporter,
no analytics, and no account.

### Why the webhook is the way it is

* **HTTPS or nothing.** A non-`https` URL is refused with an explanatory error. The
  `allow_insecure` override (`alerts.webhook.allow_insecure = true`, or
  `--allow-insecure-webhook` for one run) exists for a loopback test endpoint and
  logs a warning when used. Sending derived health data over plaintext is not
  something the program will do quietly.
* **The body is the alert, and nothing else.** No file paths, no configuration, no
  other plugins' output, no plugin labels or internal detail fields - the test
  asserts that `Alert.as_dict(full=False)` is what gets posted.
* **A failed POST changes nothing else.** It is logged, recorded in the run report,
  and the alert is still written to local history. Delivery to a third party is
  best-effort; local record-keeping is not.
* **Dedup runs before the network.** A repeat inside `alerts.cooldown` updates the
  stored row and is not sent, so one overnight episode is one outbound message.

## Local storage

| file | contains | notes |
|---|---|---|
| `<data-dir>/store.sqlite3` | raw payloads, normalized series, the dataset registry, watermarks, import history, quarantine, alert history | `0600` recommended; the one file that is your data, and the one file `fm store backup` copies |
| `<data-dir>/backups/*.sqlite3` | snapshots made by `fm store backup` | full copies of the store - protect them exactly as much as the store itself, and delete them when you want the data gone |
| `<data-dir>/credentials.json` | Google OAuth tokens for the account whose Fitbit data is being imported | written `0600`; `fm auth logout` deletes it |
| `<data-dir>/imports/*.json` | one report per import run: ranges, counts, watermarks, errors | the last `[import] report_keep` runs; contains dates and counts, not measurements |
| `<data-dir>/http-etag-cache.json` | ETags and response bodies for conditional GETs | cache only; safe to delete, costs refetches |
| `<data-dir>/alerts.jsonl` | every delivered alert, append-only | plugin, key, title, message, severity, timestamp, confidence, evidence - a summary of a finding, not a raw series; rotate or delete freely |
| whatever you pass to `fm store export` | the normalized series, or the original raw payloads | created only when you ask, and it prints where it wrote; treat the file like the store |
| `<config-dir>/config.toml` | paths, plugin options, webhook URL | contains your webhook URL - treat it as a secret if it has a token in it |

Default locations are `$XDG_DATA_HOME/fitbit-monitor` and
`$XDG_CONFIG_HOME/fitbit-monitor`, overridable with `--data-dir`, `--config`, `--db`
or `FM_DATA_DIR` / `FM_CONFIG` / `FM_DB`.

### Exclude these from cloud sync and backup

The data directory is the thing a cloud-sync client will happily upload. Exclude, on
Dropbox / OneDrive / Google Drive / iCloud / Time Machine-as-offsite-copy alike:

* `~/.local/share/fitbit-monitor/` (or `$XDG_DATA_HOME/fitbit-monitor`) - the store,
  the token file, the import reports, the ETag cache, the alert log, and `backups/`;
* `~/Library/Application Support/fitbit-monitor/` on macOS, if that is where your
  `XDG_DATA_HOME` resolves;
* `<config-dir>/fitbit-monitor/config.toml` - not health data, but it holds your webhook
  URL;
* anything you have pointed `fm store export --output` at.

Backups are not offsite-by-default: `fm store backup` writes next to the store and goes
nowhere else. If you want an offsite copy, encrypt the destination yourself (an
encrypted disk image, `restic`, `age`) - the file it produces is plaintext, like the
store it came from.

### Logs

Log output names datasets, counts, dates, durations and file paths. It does not contain
readings: every record that reaches an `fm` log handler passes a redacting filter
(`fitbit_monitor.logsafe`) that rewrites a health value in a message to
`resting_hr=<redacted>`, including values that arrive as deferred `%s` arguments. A test
asserts it (`tests/test_data_policy.py`), because a promise about logs that no test
enforces is a comment, not a guarantee. Alert titles and messages *are* summaries of
findings ("resting HR up 9 bpm over 6 weeks") - that is their purpose, and they are
local unless you configured a webhook.

**Credentials are stripped by the same mechanism**, in a second filter
(`fitbit_monitor.logsecrets`), in both the text and the `--log-format json` output:

| what | how it is caught | appears as |
|---|---|---|
| an OAuth access or refresh token | the `TokenSet` registers both the instant it is loaded, so the value itself is masked wherever it turns up | `[REDACTED]` |
| the OAuth client secret | `config.toml` registers it when the config is read; `client_secret=`/`"client_secret":` is also matched by name | `[REDACTED]` |
| an `Authorization:` header, a `password=`/`api_key=` style key, a token in a query string | matched by key name, in `k=v`, `k: v`, JSON and Python-dict shape | `Bearer [REDACTED]` (the scheme survives, the credential does not) |
| a token that appears in a traceback | the exception text goes through the same filter | `[REDACTED]` |

Redaction is central rather than per-call-site on purpose: the leak that matters is the
line nobody wrote deliberately - a debug line that interpolates a header dict, an
`HTTPError` whose message quotes a URL, a plugin that logs the config it was handed.
What a *call site* may not do is write a credential at all; what the filter guarantees is
that when one does anyway, it stays out of the file. `tests/test_log_redaction.py` asserts
it for both formats, including for a token read back from `credentials.json`.

The log level is yours: `-v`/`-vv`/`-q` or `FM_LOG_LEVEL`, with `--log-format json` for
machine-readable lines. None of those switch redaction on - it is always on, and the
filter runs before the formatter, so `--log-format json` is not a way to see a value the
text format would have hidden.

## Retention

**Default retention: keep everything.** The API's own history is finite and this store is
the copy you own; deleting by default is the wrong direction for data you cannot get back.

Where someone wants a bound, `fm store prune` implements the policy and
**its default behaviour is a dry run**:

```
fm store prune --days 365              # prints what would go; deletes nothing
fm store prune --days 365 --execute    # deletes raw payloads fetched over a year ago
fm store prune --days 365 --what all --execute --vacuum
```

* The documented default horizon for anyone who does prune is **365 days**, and `--days`
  defaults to it.
* `--what` defaults to `raw`: retained responses are the bulk of the file and the only
  part a source may still re-send. Normalized series and quarantine data are only touched
  when you name them.
* Pruning never touches watermarks, so a pruned window is still a window `fm` records as
  fetched - and re-importing it later is safe, just wasted requests.
* `--execute` is required. A mistyped retention command cannot delete a year of
  heart-rhythm history on the strength of one flag.

### Encryption at rest: deliberately not implemented

Recorded here so the question is answered rather than deferred
([ADR-0003](adr/0003-storage-model.md) §7):

* **Not implemented, on purpose.** SQLCipher means a C extension, which breaks the
  stdlib-only rule that keeps a fresh clone runnable and a plugin author's environment
  empty; it needs a key, and a key stored next to the database it protects is theatre;
  every SQLite tool and `fm store verify` check would have to go through it.
* **The threat that actually applies here** is a lost machine or a cloud-synced folder,
  and it is handled by full-disk encryption (FileVault / BitLocker / LUKS), by writing the
  store `0600`, and by the exclusion list above.
* **What would change the answer:** a deployment on a shared or multi-user machine, or a
  supported "sync my store" feature. Either one makes at-rest encryption a requirement
  rather than a trade-off.

So: the store is plaintext, the policy says so out loud, and the two mitigations that are
free (whole-disk encryption, keeping the directory out of the cloud) are the ones the
documentation tells you to do.

## Plugins

A plugin is given a read-only view of the store and no I/O capability of its own:
no writable connection (SQLite enforces it), no filesystem handle, no HTTP client,
no network helper in the API surface. If a plugin reaches for the raw connection to
write, the database refuses it.

That is a strong boundary but not a sandbox: a plugin is Python and can import
`urllib` if its author chose to. The trust level of a plugin is the trust level of
its author - which is why the contract document and the published conformance suite
exist, and why a plugin registry is a deliberate non-goal for now
([ADR-0004](adr/0004-plugin-contract.md)).

## Deletion

`fm auth logout` removes the tokens. `fm store prune --days N --execute` removes old data
(see **Retention**). To remove a whole dataset or window today, that is one SQL
`DELETE` against the store - `fm store export` first if you want the copy in hand.

Deleting `store.sqlite3`, `backups/`, `credentials.json`, `imports/`,
`http-etag-cache.json` and `alerts.jsonl` removes everything this program knows; there is
no server-side copy to delete, which is the point.

## Not covered here

The health service's own retention, and the terms under which its API grants access,
are the service's policy, not this program's. This document describes what *this
program* does with what it is given.
