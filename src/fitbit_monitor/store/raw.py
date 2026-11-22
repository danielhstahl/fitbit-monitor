"""Raw payload retention: storing the bytes the source sent, and reading them back.

The rule this module exists to keep (ADR-0003) is that **the original payload is the
record** and the normalized rows are a derived convenience. Anything here that reads a
payload therefore answers two questions the caller cannot: which dataset it belongs to,
and how the bytes are encoded - payloads written before migration 0003 are stored as
plain text, newer ones gzip, and guessing from the first byte is not a contract.

Content-addressing is what makes overlapping imports cheap: the same bytes fetched
twice (two overlapping date ranges, an API import and a CSV export of the same day)
hash to the same ``content_hash``, and the unique index means the second copy is not
stored at all.
"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator

#: Compression level, not the default 9: these blobs are JSON, level 6 gets ~95% of the
#: ratio for a third of the CPU, and `fm import` should not spend its time in gzip.
COMPRESS_LEVEL = 6
#: Below this, gzip costs more than it saves (a 60-byte error body compresses to ~70
#: bytes), so it is stored exactly as it came. Real payloads are kilobytes.
COMPRESS_MIN_BYTES = 512

ENCODING_GZIP = "gzip"
ENCODING_IDENTITY = "identity"

COLUMNS = (
    "content_hash",
    "dataset",
    "source",
    "source_id",
    "requested_start",
    "requested_end",
    "fetched_at",
    "bytes",
    "stored_bytes",
    "encoding",
)


def content_hash(body: str | bytes) -> str:
    """SHA-256 of the payload **as received**, hex. The dedup key, and what verify re-checks."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    return hashlib.sha256(data).hexdigest()


def encode_body(body: str) -> tuple[bytes, int, int, str]:
    """Return ``(blob, raw_bytes, stored_bytes, encoding)`` for a payload."""
    data = body.encode("utf-8")
    if len(data) < COMPRESS_MIN_BYTES:
        return data, len(data), len(data), ENCODING_IDENTITY
    # mtime=0 keeps stored bytes reproducible, which makes a store diffable.
    blob = gzip.compress(data, compresslevel=COMPRESS_LEVEL, mtime=0)
    if len(blob) >= len(data):  # incompressible body: keep the smaller of the two
        return data, len(data), len(data), ENCODING_IDENTITY
    return blob, len(data), len(blob), ENCODING_GZIP


def decode_body(blob: bytes | str, encoding: str) -> str:
    """The payload text back out of the stored blob.

    A str is accepted because SQLite columns are permissively typed: the copy migration
    in 0003 moved bodies out of a TEXT column, so a migrated store can hand back text
    where a new store hands back bytes.
    """
    if encoding == ENCODING_IDENTITY:
        return blob if isinstance(blob, str) else bytes(blob).decode("utf-8")
    if encoding == ENCODING_GZIP and not isinstance(blob, str):
        return gzip.decompress(blob).decode("utf-8")
    raise ValueError(f"unknown payload encoding {encoding!r}")


@dataclass(frozen=True)
class RawPayload:
    """One stored response: its metadata, and (optionally) the bytes themselves."""

    content_hash: str
    dataset: str
    source: str
    source_id: str
    requested_start: str | None
    requested_end: str | None
    fetched_at: str
    bytes: int
    stored_bytes: int
    encoding: str = ENCODING_GZIP
    body: str | None = None

    @property
    def ratio(self) -> float:
        """Stored size over original size - how well this payload compressed."""
        if not self.bytes:  # pragma: no cover - an empty body is not worth a ratio
            return 1.0
        return self.stored_bytes / self.bytes

    def as_dict(self, *, include_body: bool = True) -> dict[str, object]:
        payload = {name: getattr(self, name) for name in COLUMNS}
        payload["compression_ratio"] = round(self.ratio, 4)
        if include_body:
            payload["body"] = self.body
        return payload

    @classmethod
    def from_row(cls, row: sqlite3.Row, *, with_body: bool = True) -> "RawPayload":
        keys = row.keys()
        body = None
        if with_body and "body" in keys and row["body"] is not None:
            body = decode_body(row["body"], row["encoding"])
        return cls(
            content_hash=row["content_hash"],
            dataset=row["dataset"],
            source=row["source"],
            source_id=row["source_id"],
            requested_start=row["requested_start"],
            requested_end=row["requested_end"],
            fetched_at=row["fetched_at"],
            bytes=int(row["bytes"]),
            stored_bytes=int(row["stored_bytes"]),
            encoding=row["encoding"],
            body=body,
        )


def _iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def iter_payloads(
    conn: sqlite3.Connection,
    *,
    dataset: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    source: str | None = None,
    limit: int | None = None,
    with_body: bool = True,
) -> Iterator[RawPayload]:
    """Stored payloads, oldest fetched first, optionally narrowed to a window.

    The window filters on ``fetched_at`` (when it was fetched), not on the range the
    payload covers: a payload for a week is one row, and its coverage is
    ``requested_start``/``requested_end``.
    """
    sql = f"SELECT {'body, ' if with_body else ''}{', '.join(COLUMNS)} FROM raw_payload"
    where: list[str] = []
    params: list[object] = []
    if dataset:
        where.append("dataset = ?")
        params.append(dataset)
    if source:
        where.append("source = ?")
        params.append(source)
    if since is not None:
        where.append("fetched_at >= ?")
        params.append(_iso(since))
    if until is not None:
        where.append("fetched_at < ?")
        params.append(_iso(until))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY fetched_at, rowid"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    for row in conn.execute(sql, params):
        yield RawPayload.from_row(row, with_body=with_body)


def payload_by_hash(conn: sqlite3.Connection, digest: str, *, with_body: bool = True) -> RawPayload | None:
    row = conn.execute(
        f"SELECT {'body, ' if with_body else ''}{', '.join(COLUMNS)} FROM raw_payload WHERE content_hash = ?",
        (digest,),
    ).fetchone()
    return RawPayload.from_row(row, with_body=with_body) if row else None


def totals(conn: sqlite3.Connection, *, dataset: str | None = None) -> dict[str, dict[str, float]]:
    """Per-dataset payload counts and bytes, raw and stored (``"all"`` included)."""
    sql = (
        "SELECT dataset, COUNT(*) AS payloads, COALESCE(SUM(bytes), 0) AS bytes, "
        "COALESCE(SUM(stored_bytes), 0) AS stored_bytes FROM raw_payload"
    )
    params: list[object] = []
    if dataset:
        sql += " WHERE dataset = ?"
        params.append(dataset)
    sql += " GROUP BY dataset ORDER BY dataset"
    out: dict[str, dict[str, float]] = {}
    all_bytes = all_stored = all_count = 0
    for row in conn.execute(sql, params):
        out[row["dataset"]] = {
            "payloads": int(row["payloads"]),
            "bytes": int(row["bytes"]),
            "stored_bytes": int(row["stored_bytes"]),
        }
        all_count += int(row["payloads"])
        all_bytes += int(row["bytes"])
        all_stored += int(row["stored_bytes"])
    out["all"] = {"payloads": all_count, "bytes": all_bytes, "stored_bytes": all_stored}
    return out


__all__ = [
    "COMPRESS_LEVEL",
    "COLUMNS",
    "ENCODING_GZIP",
    "ENCODING_IDENTITY",
    "RawPayload",
    "content_hash",
    "decode_body",
    "encode_body",
    "iter_payloads",
    "payload_by_hash",
    "totals",
]
