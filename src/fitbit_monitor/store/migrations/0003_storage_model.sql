-- 0003 - storage model (ADR-0003): raw payload retention, the dataset registry,
-- typed per-dataset views, and the dev-only annotation table.
--
-- Forward-only, like every file here. The raw_payload rebuild is the one part that
-- rewrites existing rows: payloads move from a TEXT column keyed (dataset, hash) to a
-- gzip BLOB keyed by content hash alone, so the same bytes fetched twice - by two
-- overlapping imports, from two sources - are stored once.

-- 1. The dataset registry: what the store holds, and how it is shaped. Written by the
--    writer as it stores data; the read API answers "what is in here?" from it.
CREATE TABLE IF NOT EXISTS dataset (
    name                TEXT PRIMARY KEY,
    kind                TEXT NOT NULL DEFAULT 'daily' CHECK (kind IN ('intraday', 'daily')),
    granularity_seconds INTEGER,
    first_seen_at       TEXT
);

-- Datasets already in the store, with the grain their catalog entry declares.
INSERT OR IGNORE INTO dataset (name, kind, granularity_seconds)
SELECT DISTINCT dataset,
       CASE WHEN dataset IN ('heart_rate', 'heart_rate_intraday') THEN 'intraday' ELSE 'daily' END,
       CASE WHEN dataset IN ('heart_rate', 'heart_rate_intraday') THEN 60 ELSE 86400 END
  FROM series;

-- A payload can exist for a dataset that has no rows yet (all of it failed to
-- normalize), so the registry has to know it too - the FK below depends on that.
INSERT OR IGNORE INTO dataset (name, kind, granularity_seconds)
SELECT DISTINCT dataset, 'daily', NULL FROM raw_payload
 WHERE dataset NOT IN (SELECT name FROM dataset);

-- 2. Raw payloads: content-addressed and compressed, with the range that was asked
--    for so a re-import can prove what it covers.
ALTER TABLE raw_payload RENAME TO raw_payload_v1;

CREATE TABLE raw_payload (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash    TEXT NOT NULL UNIQUE,
    dataset         TEXT NOT NULL REFERENCES dataset(name) ON DELETE CASCADE,
    source          TEXT NOT NULL DEFAULT 'api',
    source_id       TEXT NOT NULL DEFAULT '',
    requested_start TEXT,
    requested_end   TEXT,
    fetched_at      TEXT NOT NULL,
    bytes           INTEGER NOT NULL DEFAULT 0,
    stored_bytes    INTEGER NOT NULL DEFAULT 0,
    encoding        TEXT NOT NULL DEFAULT 'gzip' CHECK (encoding IN ('gzip', 'identity')),
    body            BLOB NOT NULL
);

-- Pre-existing payloads were stored uncompressed; they stay readable because every
-- reader looks at `encoding` before decompressing.
INSERT OR IGNORE INTO raw_payload
    (content_hash, dataset, source, source_id, fetched_at, bytes, stored_bytes, encoding, body)
SELECT content_hash, dataset, 'api', source_id, fetched_at,
       length(body), length(body), 'identity', body
  FROM raw_payload_v1
 GROUP BY content_hash;

DROP TABLE raw_payload_v1;

-- "every payload fetched for dataset X between t1 and t2", the only raw query that
-- matters, plus the newest-first listing `fm store stats` shows.
CREATE INDEX IF NOT EXISTS raw_payload_dataset_fetched ON raw_payload (dataset, fetched_at DESC);
CREATE INDEX IF NOT EXISTS raw_payload_fetched ON raw_payload (fetched_at DESC);

-- 3. One typed view per dataset over the normalized series. The single `series` table
--    is the storage (ADR-0003: one normalization path, one dedup key); the views are
--    the typed columnar surface, so SQL against the store reads `heart_rate_bpm`
--    rather than json_extract, and a new dataset costs a view rather than a copy.
CREATE VIEW IF NOT EXISTS v_heart_rate_intraday AS
SELECT ts_utc AS ts,
       json_extract(payload, '$.hr') AS heart_rate_bpm,
       source_id
  FROM series WHERE dataset = 'heart_rate';

CREATE VIEW IF NOT EXISTS v_daily_summary AS
SELECT substr(ts_utc, 1, 10) AS day,
       json_extract(payload, '$.resting_hr') AS resting_hr_bpm,
       json_extract(payload, '$.steps') AS steps,
       json_extract(payload, '$.calories') AS calories_kcal,
       json_extract(payload, '$.active_minutes') AS active_minutes,
       source_id
  FROM series WHERE dataset = 'daily_summary';

CREATE VIEW IF NOT EXISTS v_sleep AS
SELECT substr(ts_utc, 1, 10) AS day,
       json_extract(payload, '$.sleep_minutes') AS sleep_minutes,
       json_extract(payload, '$.awake_minutes') AS awake_minutes,
       json_extract(payload, '$.deep_minutes') AS deep_minutes,
       source_id
  FROM series WHERE dataset = 'sleep';

CREATE VIEW IF NOT EXISTS v_hrv AS
SELECT substr(ts_utc, 1, 10) AS day,
       json_extract(payload, '$.rmssd') AS rmssd_ms,
       json_extract(payload, '$.sdnn') AS sdnn_ms,
       json_extract(payload, '$.hrv_n') AS hrv_n,
       source_id
  FROM series WHERE dataset = 'hrv';

-- 4. Day-scale index: coverage and "which days do I have" group by the date prefix of
--    the instant, which the (dataset, ts_utc) primary key cannot answer without
--    reading every row.
CREATE INDEX IF NOT EXISTS series_dataset_day ON series (dataset, substr(ts_utc, 1, 10));

-- The run log records raw-payload duplicates too, so `fm store verify` and
-- `fm store stats` can report what an import refused to store twice rather than making
-- that up from the raw table (where a duplicate was never written at all).
ALTER TABLE dataset_run ADD COLUMN payloads_duplicate INTEGER NOT NULL DEFAULT 0;

-- 5. Labels for the synthetic corpus only. Nothing that comes from a real account is
--    ever written here; `fm dev seed` injects episodes and records them so detector
--    tests can assert on ground truth.
CREATE TABLE IF NOT EXISTS dev_annotation (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    dataset TEXT NOT NULL REFERENCES dataset(name) ON DELETE CASCADE,
    start_ts TEXT NOT NULL,
    end_ts   TEXT NOT NULL,
    label   TEXT NOT NULL,
    seed    INTEGER,
    details TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details)),
    UNIQUE (kind, dataset, start_ts, end_ts, label)
);

CREATE INDEX IF NOT EXISTS dev_annotation_window ON dev_annotation (dataset, start_ts);
