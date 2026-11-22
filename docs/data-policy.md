# Data policy: what leaves this machine, and what never does

fitbit-monitor is local-first. This document is the short, checkable statement of
the data policy - the one that the webhook code, the plugin contract and the tests
are written to agree with.

## The rule

**Your health data stays on this machine.** The only outbound network request the
program can make is the alert webhook you configure, and it sends only the alert.

## In

| direction | what | where it goes |
|---|---|---|
| ingest | your Fitbit data (API import or a CSV export) | the local SQLite store |
| read | plugins, `fm` commands, the read API | local only |

Nothing else in the program opens a socket. Not the store, not the plugin runner,
not the alert history, not the benchmarks.

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
| `<data-dir>/store.sqlite3` | raw payloads, normalized series, alert history | `0600` recommended; the encryption question is tracked in the storage epic |
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

`fm store prune` (storage epic) and, today, plain SQL against the store. Deleting
`store.sqlite3` and `alerts.jsonl` removes everything this program knows; there is
no server-side copy to delete, which is the point.

## Not covered here

Fitbit's own retention and the terms under which the API grants access are the
service's policy, not this program's. This document describes what *this program*
does with what it is given.
