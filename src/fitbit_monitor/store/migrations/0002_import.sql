-- 0002 - the ad-hoc import epic (fitb-bmp): where a dataset got to, what each import
-- run did, and the payloads that were refused.

CREATE TABLE IF NOT EXISTS dataset_watermark (
    dataset    TEXT NOT NULL,
    source     TEXT NOT NULL,
    last_day   TEXT,
    last_ts    TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset, source)
);

CREATE TABLE IF NOT EXISTS dataset_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    dataset       TEXT NOT NULL,
    source        TEXT NOT NULL,
    since         TEXT,
    until         TEXT,
    status        TEXT NOT NULL,
    chunks        INTEGER NOT NULL DEFAULT 0,
    requests      INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_duplicate INTEGER NOT NULL DEFAULT 0,
    quarantined   INTEGER NOT NULL DEFAULT 0,
    bytes_stored  INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    watermark     TEXT,
    error         TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS dataset_run_run ON dataset_run (run_id, dataset);

CREATE INDEX IF NOT EXISTS dataset_run_started ON dataset_run (started_at DESC);

CREATE TABLE IF NOT EXISTS quarantine (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset        TEXT NOT NULL,
    source_id      TEXT,
    reason         TEXT NOT NULL,
    body           TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS quarantine_dataset ON quarantine (dataset, quarantined_at DESC);
