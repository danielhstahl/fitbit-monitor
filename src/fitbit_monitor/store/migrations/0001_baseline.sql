-- 0001 - baseline schema.
--
-- The store as it shipped with the plugin system: one normalized series table with
-- JSON payloads, a raw-payload table so nothing ingested is thrown away, and the
-- alert history that the alert sinks write. This file is history: never edit it,
-- add a numbered file instead (ADR-0003, "migrations are forward-only").

CREATE TABLE IF NOT EXISTS fm_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_payload (
    dataset     TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    body        TEXT NOT NULL,
    PRIMARY KEY (dataset, content_hash)
);

CREATE TABLE IF NOT EXISTS series (
    dataset  TEXT NOT NULL,
    ts_utc   TEXT NOT NULL,
    payload  TEXT NOT NULL,
    source_id TEXT,
    PRIMARY KEY (dataset, ts_utc)
);

CREATE INDEX IF NOT EXISTS series_ds_ts ON series (dataset, ts_utc);

CREATE TABLE IF NOT EXISTS alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key        TEXT NOT NULL,
    plugin           TEXT NOT NULL,
    key              TEXT NOT NULL,
    severity         TEXT NOT NULL,
    title            TEXT NOT NULL,
    message          TEXT NOT NULL,
    confidence       REAL,
    occurred_at      TEXT NOT NULL,
    window_start     TEXT,
    window_end       TEXT,
    evidence         TEXT NOT NULL DEFAULT '[]',
    first_emitted_at TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    occurrences      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS alerts_dedup ON alerts (dedup_key, last_seen_at);

CREATE INDEX IF NOT EXISTS alerts_seen ON alerts (last_seen_at);
