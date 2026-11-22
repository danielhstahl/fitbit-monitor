"""Downsampling helpers - the second half of the read API.

Minute-level heart rate is 30k points a week; a weekly trend plugin does not want
to think about that. ``downsample`` buckets a point iterator by hour/day/week and
collapses each bucket with a named aggregate, returning the same ``SeriesPoint``
shape so plugin code reads the same whether it is working intraday or weekly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import fmean, median
from typing import Iterable, Iterator, Sequence

from ..errors import UsageError
from .reader import SeriesPoint

AGGREGATES = ("mean", "median", "min", "max", "sum", "first", "last", "count")


def aggregate(values: Sequence[float], how: str = "mean") -> float | None:
    """Collapse ``values`` with ``how``. Returns ``None`` for an empty sequence."""
    if not values:
        return None
    if how == "mean":
        return float(fmean(values))
    if how == "median":
        return float(median(values))
    if how == "min":
        return float(min(values))
    if how == "max":
        return float(max(values))
    if how == "sum":
        return float(sum(values))
    if how == "first":
        return float(values[0])
    if how == "last":
        return float(values[-1])
    if how == "count":
        return float(len(values))
    raise UsageError(f"unknown aggregate {how!r}", hint=f"pick one of: {', '.join(AGGREGATES)}")


def bucket_key(ts: datetime, period: str) -> datetime:
    """Truncate ``ts`` to the start of its ``period`` bucket (UTC)."""
    if period == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    if period in ("day", "daily"):
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if period in ("week", "weekly"):
        day = ts.date() - timedelta(days=ts.weekday())
        return datetime(day.year, day.month, day.day, tzinfo=ts.tzinfo)
    if period in ("month", "monthly"):
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period in ("", "none", None):  # type: ignore[comparison-overlap]
        return ts
    raise UsageError(f"unknown period {period!r}", hint="use hour, day, week or month")


def downsample(
    points: Iterable[SeriesPoint],
    period: str = "day",
    *,
    how: str = "mean",
    fields: Sequence[str] | None = None,
) -> Iterator[SeriesPoint]:
    """Bucket points by ``period`` and aggregate each numeric field with ``how``.

    Buckets come out in chronological order. A field that is absent from every
    point in a bucket is absent from the bucket, so callers can tell "no data"
    from "zero".
    """
    grouped: dict[datetime, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    wanted = set(fields) if fields else None
    for point in points:
        key = bucket_key(point.ts, period)
        for name, reading in point.values.items():
            if wanted and name not in wanted:
                continue
            if reading is None:
                continue
            grouped[key][name].append(float(reading))
    for key in sorted(grouped):
        collapsed: dict[str, float] = {}
        for name, values in grouped[key].items():
            value = aggregate(values, how)
            if value is not None:
                collapsed[name] = value
        if collapsed:
            yield SeriesPoint(ts=key, values=collapsed)


def daily_values(
    points: Iterable[SeriesPoint], field: str, *, how: str = "mean"
) -> dict[date, float]:
    """Convenience: ``{date: aggregated field}`` for one field. Days with no value are absent."""
    out: dict[date, float] = {}
    for bucket in downsample(points, "day", how=how, fields=[field]):
        value = bucket.get(field)
        if value is not None:
            out[bucket.ts.date()] = value
    return out
