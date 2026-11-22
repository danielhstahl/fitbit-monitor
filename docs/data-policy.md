# Data policy: what leaves this machine, and what never does

fitbit-monitor is local-first. This document is the short, checkable statement of
the data policy - the one that the webhook code, the plugin contract and the tests
are written to agree with.

## The rule

**Your health data stays on this machine.** The program talks to exactly two
destinations, both because you asked: the Fitbit API, when and only when you run
`fm auth` or `fm import`, and the alert webhook you configure. There is no third
party, no background service, and no telemetry.

## In

| direction | what | where it goes |
|---|---|---|
| ingest | your Fitbit data (API import or a CSV export) | the local SQLite store |
| read | plugins, `fm` commands, the read API | local only |

### What the API call sends

Nothing you did not choose, and only while an import is running:

| request | sent | why |
|---|---|---|
| `fm auth login` / token refresh | your PKCE verifier and authorization code, then the refresh token | to get and renew a token; to `auth.fitbit.com` |
| `fm import` | `Authorization: Bearer <token>`, the date range you asked for, and an `If-None-Match` ETag when we have one | to fetch the days you named; to `[api] base_url` (default `https://api.fitbit.com`) |

Plain HTTP is refused for the API and for the webhook - `https` or the loopback
exception, never a plaintext internet hop. No query, metric or stored value is ever
sent *out* to Fitbit; the calls are reads.

Nothing else in the program opens a socket. Not the store, not the plugin runner,
not the alert history, not the benchmarks. An import is the only thing that
initiates contact with Fitbit, and `--fixtures DIR` runs the whole path with no
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
| `<data-dir>/store.sqlite3` | raw payloads, normalized series, alert history, import history and quarantine | `0600` recommended; the encryption question is tracked in the storage epic |
| `<data-dir>/credentials.json` | Fitbit access and refresh tokens | written `0600`; `fm auth logout` deletes it |
| `<data-dir>/imports/*.json` | one report per import run: ranges, counts, watermarks, errors | the last `[import] report_keep` runs; contains dates and counts, not measurements |
| `<data-dir>/http-etag-cache.json` | ETags and response bodies for conditional GETs | cache only; safe to delete, costs refetches |
| `<data-dir>/alerts.jsonl` | every delivered alert, append-only | rotate or delete freely; it is a convenience copy |
| `<config-dir>/config.toml` | paths, plugin options, webhook URL | contains your webhook URL - treat it as a secret if it has a token in it |

Default locations are `$XDG_DATA_HOME/fitbit-monitor` and
`$XDG_CONFIG_HOME/fitbit-monitor`, overridable with `--data-dir`, `--config`, `--db`
or `FM_DATA_DIR` / `FM_CONFIG` / `FM_DB`.

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

`fm auth logout` removes the tokens. `fm store prune` (storage epic) and, today,
plain SQL against the store. Deleting `store.sqlite3`, `credentials.json`,
`imports/`, `http-etag-cache.json` and `alerts.jsonl` removes everything this
program knows; there is no server-side copy to delete, which is the point.

## Not covered here

Fitbit's own retention and the terms under which the API grants access are the
service's policy, not this program's. This document describes what *this program*
does with what it is given.
