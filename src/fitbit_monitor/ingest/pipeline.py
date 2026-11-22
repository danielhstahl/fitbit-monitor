"""The idempotent ingest path: raw retention, row upsert, quarantine.

Everything that ends up in the store goes through :meth:`Ingest.payload`, whether it
came from the API, from a CSV export, or from a future source. One door means one
set of guarantees, and the guarantees are what make "run it again" safe:

**raw first.** The response body is kept verbatim, keyed by content hash, before any
of it is normalised. Normalisation can be fixed later; a payload that was thrown away
cannot be.

**rows are upserts.** Inserted vs already-present is counted per row so a report can
tell "the API gave me 4 032 points and 4 000 of them were already here" apart from a
pull that actually added something.

**bad payloads are quarantined, not dropped.** Anything we cannot parse or normalise
goes to the ``quarantine`` table with a reason, and is counted in the report - a
silent skip is the failure mode an import tool must not have.

**one transaction per chunk.** A chunk commits whole or not at all, which is what lets
Ctrl-C leave the store consistent (see :mod:`fitbit_monitor.ingest.importer`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from ..timeutil import ensure_utc, utcnow

Row = tuple[datetime, Mapping[str, float]]

#: Above this span, "which of these instants already exist?" is answered per row
#: instead of by one range query, so a pathological call cannot materialise years of
#: timestamps in memory. Ordinary callers pass a chunk (days, not years).
_RANGE_QUERY_MAX_DAYS = 400


@dataclass
class IngestStats:
    """What one ingest call did. Additive, so chunks and datasets can be summed."""

    payloads: int = 0
    payloads_duplicate: int = 0
    payloads_quarantined: int = 0
    rows_inserted: int = 0
    rows_duplicate: int = 0
    bytes_stored: int = 0
    reasons: list[str] = field(default_factory=list)

    def __add__(self, other: "IngestStats") -> "IngestStats":
        merged = IngestStats(
            payloads=self.payloads + other.payloads,
            payloads_duplicate=self.payloads_duplicate + other.payloads_duplicate,
            payloads_quarantined=self.payloads_quarantined + other.payloads_quarantined,
            rows_inserted=self.rows_inserted + other.rows_inserted,
            rows_duplicate=self.rows_duplicate + other.rows_duplicate,
            bytes_stored=self.bytes_stored + other.bytes_stored,
            reasons=list(self.reasons) + list(other.reasons),
        )
        return merged

    def as_dict(self) -> dict[str, Any]:
        return {
            "payloads": self.payloads,
            "payloads_duplicate": self.payloads_duplicate,
            "payloads_quarantined": self.payloads_quarantined,
            "rows_inserted": self.rows_inserted,
            "rows_duplicate": self.rows_duplicate,
            "bytes_stored": self.bytes_stored,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Chunk:
    """The unit of durability: everything fetched between two commits."""

    dataset: str
    days: tuple[Any, ...] = ()
    label: str = ""


class Ingest:
    """Writable ingest on top of a :class:`~fitbit_monitor.store.writer.SeriesStore`."""

    def __init__(self, store, *, source: str = "api", clock=utcnow) -> None:
        self.store = store
        self.source = source
        self.clock = clock

    def payload(
        self,
        dataset: str,
        source_id: str,
        body: str,
        rows: Sequence[Row] | None = None,
        *,
        chunk: Chunk | None = None,
        requested_start: Any = None,
        requested_end: Any = None,
        commit: bool = True,
    ) -> IngestStats:
        """Store one raw payload and the rows normalised from it.

        Raises nothing for bad content: an unparseable body or an invalid row is
        quarantined and reported. Pass ``commit=False`` to fold several payloads into
        one chunk transaction and :meth:`commit` later.

        ``requested_start``/``requested_end`` are the window this response was asked to
        cover; with neither, the chunk's own days are used. They are stored with the
        payload so "what do I already have, and what was it for?" is answerable from the
        raw table alone.
        """
        stats = IngestStats()
        if chunk is not None and requested_start is None and chunk.days:
            requested_start = min(chunk.days)
            requested_end = requested_end if requested_end is not None else max(chunk.days)
        if self.store.put_raw(
            dataset,
            source_id,
            self.clock(),
            body,
            source=self.source,
            requested_start=requested_start,
            requested_end=requested_end,
            commit=False,
        ):
            stats.payloads = 1
            stats.bytes_stored = len(body.encode("utf-8"))
        else:
            stats.payloads_duplicate = 1
        if rows is None:
            stats.reasons.append(f"{source_id}: unparseable payload")
            self.store.quarantine(dataset, source_id, "body was not valid JSON", body)
            stats.payloads_quarantined = 1
        else:
            stats = stats + self.rows(dataset, rows, source_id=source_id)
        if commit:
            self.store.commit()
        return stats

    def rows(self, dataset: str, rows: Iterable[Row], *, source_id: str | None = None) -> IngestStats:
        """Upsert normalized rows, quarantining any row that is not well formed.

        Whether each instant is new or already there is answered by one range query
        rather than one query per row: ``rows`` is called with a chunk (a few days of
        one dataset), and a million per-row SELECTs is how an import spends its life.
        """
        stats = IngestStats()
        good: list[tuple[datetime, Mapping[str, float]]] = []
        for ts, values in rows:
            problem = _row_problem(ts, values)
            if problem:
                stats.payloads_quarantined += 1
                stats.reasons.append(f"{dataset}: {problem}")
                self.store.quarantine(
                    dataset, source_id, problem, _safe_json({"ts": str(ts), "values": dict(values)})
                )
                continue
            good.append((ts, values))
        known = self._known_instants(dataset, [ts for ts, _ in good])
        for ts, values in good:
            self.store.put(dataset, ts, values, source_id=source_id)
            stamp = ts.isoformat()
            if stamp in known:
                stats.rows_duplicate += 1
            else:
                stats.rows_inserted += 1
                known.add(stamp)  # a repeat inside the same chunk is still one row
        return stats

    def _known_instants(self, dataset: str, instants: Sequence[datetime]) -> set[str]:
        if not instants:
            return set()
        since, until = min(instants), max(instants)
        if (until - since).days > _RANGE_QUERY_MAX_DAYS:
            # A absurdly wide call is not worth materialising the whole range for.
            return {ts.isoformat() for ts in instants if self.store.payload_at(dataset, ts) is not None}
        return self.store.instants_between(dataset, since, until)

    def commit(self) -> None:
        """End a chunk: the moment an interrupt becomes safe."""
        self.store.commit()

    def rollback(self) -> None:
        """Drop an unfinished chunk, leaving everything already committed alone."""
        self.store.conn.rollback()

    # --- inspection ---------------------------------------------------------
    def quarantine_count(self, dataset: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM quarantine"
        params: list[object] = []
        if dataset:
            sql += " WHERE dataset = ?"
            params.append(dataset)
        return int(self.store.conn.execute(sql, params).fetchone()["n"])

    def recent_quarantined(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            "SELECT dataset, source_id, reason, quarantined_at FROM quarantine "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def _row_problem(ts: Any, values: Any) -> str | None:
    if not isinstance(ts, datetime):
        return f"row timestamp is {type(ts).__name__}, not a datetime"
    try:
        ensure_utc(ts, field="ts")
    except (ValueError, TypeError) as exc:
        return f"row timestamp is not timezone-aware: {exc}"
    if not isinstance(values, Mapping) or not values:
        return "row carries no values"
    for key, value in values.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"field {key!r} is not numeric ({value!r})"
    return None


def _safe_json(payload: Mapping[str, Any]) -> str:
    import json

    try:
        return json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(payload)


__all__ = ["Chunk", "Ingest", "IngestStats", "Row"]
