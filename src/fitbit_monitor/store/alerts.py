"""Alert history: the ``alerts`` table, with dedup and cooldown.

An alert is *new* if no row with the same dedup key was seen within the cooldown
window; otherwise the existing row is updated (``occurrences += 1``,
``last_seen_at`` moved) and the caller suppresses outbound delivery. That is what
stops one overnight arrhythmia episode from becoming 300 webhook posts, while
leaving the history honest: the row records that it recurred.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from ..errors import StoreError
from ..plugins.models import Alert, Evidence, Severity
from ..timeutil import ensure_utc


class AlertHistory:
    """Queryable alert history stored in the same SQLite file as the series."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: Path | str) -> "AlertHistory":
        from .schema import connect_rw

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(connect_rw(path))

    def close(self) -> None:
        self.conn.close()

    # --- writing ------------------------------------------------------------
    def record(self, alert: Alert, cooldown: timedelta = timedelta(hours=36)) -> tuple[Alert, bool]:
        """Store ``alert``. Returns ``(stored_alert, is_new)``; ``is_new`` is what a sink obeys.

        Three cases, all of them chosen so that re-running a monitor is safe:

        * nothing recorded for this dedup key yet - **new**, notify;
        * the key is known and ``occurred_at`` is not after what we already stored -
          a **replay** of a range that was already processed. Not new, and the
          occurrence count does not move, so ``fm plugins run`` over last month
          twice does not double-count anything;
        * the key is known and ``occurred_at`` is after the stored one but within the
          cooldown - the **same episode again**: update the row, bump ``occurrences``,
          and do not notify again.
        """
        occurred = ensure_utc(alert.occurred_at, field="alert.occurred_at")
        row = self.conn.execute(
            "SELECT id, first_emitted_at, occurrences, last_seen_at FROM alerts "
            "WHERE dedup_key = ? ORDER BY last_seen_at DESC LIMIT 1",
            (alert.effective_dedup_key,),
        ).fetchone()
        if row is not None:
            last_seen = _parse(row["last_seen_at"])
            if last_seen is not None and occurred <= last_seen:
                return (
                    replace(
                        alert,
                        alert_id=int(row["id"]),
                        first_emitted_at=_parse(row["first_emitted_at"]),
                        last_seen_at=last_seen,
                        occurrences=int(row["occurrences"]),
                    ),
                    False,
                )
            if last_seen is not None and occurred - last_seen <= cooldown:
                self.conn.execute(
                    "UPDATE alerts SET last_seen_at = ?, occurrences = occurrences + 1, severity = ?, "
                    "confidence = COALESCE(?, confidence), evidence = ? WHERE id = ?",
                    (
                        occurred.isoformat(),
                        alert.severity.value,
                        alert.confidence,
                        json.dumps([e.as_dict() for e in alert.evidence], sort_keys=True),
                        row["id"],
                    ),
                )
                self.conn.commit()
                return (
                    replace(
                        alert,
                        alert_id=int(row["id"]),
                        first_emitted_at=_parse(row["first_emitted_at"]),
                        last_seen_at=occurred,
                        occurrences=int(row["occurrences"]) + 1,
                    ),
                    False,
                )
        first_emitted = ensure_utc(alert.first_emitted_at or occurred, field="first_emitted_at")
        cur = self.conn.execute(
            "INSERT INTO alerts (dedup_key, plugin, key, severity, title, message, confidence, occurred_at, "
            "window_start, window_end, evidence, first_emitted_at, last_seen_at, occurrences) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                alert.effective_dedup_key,
                alert.plugin,
                alert.key,
                alert.severity.value,
                alert.title,
                alert.message,
                alert.confidence,
                occurred.isoformat(),
                alert.window_start.isoformat() if alert.window_start else None,
                alert.window_end.isoformat() if alert.window_end else None,
                json.dumps([e.as_dict() for e in alert.evidence], sort_keys=True),
                first_emitted.isoformat(),
                occurred.isoformat(),
            ),
        )
        self.conn.commit()
        return (
            replace(
                alert,
                alert_id=int(cur.lastrowid or 0),
                first_emitted_at=first_emitted,
                last_seen_at=occurred,
                occurrences=1,
            ),
            True,
        )

    # --- reading ------------------------------------------------------------
    def list(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        severity: Severity | str | None = None,
        plugin: str | None = None,
        limit: int | None = 200,
        order: str = "desc",
    ) -> list[Alert]:
        """Alerts newest-first, filtered by window / severity floor / plugin."""
        sql = "SELECT * FROM alerts WHERE 1=1"
        params: list[object] = []
        if since is not None:
            sql += " AND last_seen_at >= ?"
            params.append(ensure_utc(since, field="since").isoformat())
        if until is not None:
            sql += " AND last_seen_at < ?"
            params.append(ensure_utc(until, field="until").isoformat())
        if plugin:
            sql += " AND plugin = ?"
            params.append(plugin)
        if severity is not None:
            allowed = _severities_at_least(Severity.parse(severity))
            sql += f" AND severity IN ({', '.join('?' * len(allowed))})"
            params.extend(allowed)
        sql += " ORDER BY last_seen_at " + ("ASC" if order.lower() == "asc" else "DESC")
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._to_alert(r) for r in self.conn.execute(sql, params)]

    def latest(self, dedup_key: str | None = None) -> Alert | None:
        """The most recently seen alert, optionally restricted to one dedup key."""
        if dedup_key is None:
            rows = self.list(limit=1)
            return rows[0] if rows else None
        matches = [a for a in self.list(limit=1000) if a.effective_dedup_key == dedup_key]
        return matches[0] if matches else None

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"])

    def occurrences(self, dedup_key: str) -> int:
        """Total times an alert key has been recorded, including collapsed repeats."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(occurrences), 0) AS n FROM alerts WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    @staticmethod
    def _to_alert(row: sqlite3.Row) -> Alert:
        raw = dict(row)
        return Alert(
            plugin=raw["plugin"],
            key=raw["key"],
            title=raw["title"],
            message=raw["message"],
            occurred_at=_require_parse(raw["occurred_at"], "occurred_at"),
            severity=Severity.parse(raw["severity"]),
            confidence=raw["confidence"],
            evidence=tuple(Evidence.from_dict(e) for e in json.loads(raw["evidence"] or "[]")),
            window_start=_parse(raw["window_start"]) if raw["window_start"] else None,
            window_end=_parse(raw["window_end"]) if raw["window_end"] else None,
            dedup_key=raw["dedup_key"],
            alert_id=int(raw["id"]),
            first_emitted_at=_parse(raw["first_emitted_at"]),
            last_seen_at=_parse(raw["last_seen_at"]),
            occurrences=int(raw["occurrences"]),
        )


def _severities_at_least(severity: Severity) -> tuple[str, ...]:
    return tuple(
        s.value for s in (Severity.INFO, Severity.WARNING, Severity.CRITICAL) if s.rank >= severity.rank
    )


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise StoreError(f"stored timestamp {text!r} is not timezone-aware")
    return value.astimezone(value.tzinfo)


def _require_parse(text: str, field: str) -> datetime:
    value = _parse(text)
    if value is None:
        raise StoreError(f"alert row is missing {field}")
    return value


__all__ = ["AlertHistory"]
