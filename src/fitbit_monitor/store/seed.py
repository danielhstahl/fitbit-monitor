"""Deterministic synthetic data - ``fm dev seed``.

Three reasons this exists here rather than in a test folder: plugin authors need
sample data before they have real data, the benchmark job needs a fixed corpus,
and the plugin contract test suite (``fitbit_monitor.plugins.testing``) is built on
it so a third-party plugin can be tested the same way ours are.

It is *deterministic*: same ``seed``, same end date, same bytes in the store. That
is what makes a benchmark baseline meaningful and a flaky test visible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Iterator

from .writer import SeriesStore

#: What the generator writes, and which numeric fields each dataset carries.
DATASETS: dict[str, tuple[str, ...]] = {
    "heart_rate": ("hr",),
    "daily_summary": ("resting_hr", "steps", "calories", "active_minutes"),
    "hrv": ("rmssd", "sdnn", "hrv_n"),
    "sleep": ("sleep_minutes", "awake_minutes", "deep_minutes"),
    "spo2": ("spo2_min", "spo2_avg"),
}

INTRADAY_STEP = timedelta(minutes=5)


@dataclass(frozen=True)
class SeedPlan:
    """What :func:`seed_store` will write - returned so callers can report it."""

    days: int
    start: date
    end: date
    seed: int
    points: int
    dropped_days: tuple[date, ...]
    per_dataset: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "days": self.days,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "seed": self.seed,
            "points": self.points,
            "dropped_days": [d.isoformat() for d in self.dropped_days],
            "per_dataset": dict(self.per_dataset),
        }


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _resting_baseline(day_index: int) -> float:
    """A plausible resting HR: gentle drift plus a weekly rhythm."""
    return 57.0 + 2.4 * (day_index / max(1, 90)) + 1.8 * ((-1) ** (day_index % 7))


def _intraday(day: date, day_index: int, rng: random.Random) -> Iterator[tuple[datetime, dict[str, float]]]:
    resting = _resting_baseline(day_index) + rng.uniform(-1.2, 1.2)
    exercise_at = rng.randrange(6, 19) if rng.random() < 0.55 else None
    exercise_len = rng.randrange(5, 10)  # slots of 5 minutes => 25-45 min
    slot = 0
    t = _day_start(day)
    while t.time() < time(23, 55):
        hour = t.hour + t.minute / 60.0
        if hour < 5.5 or hour >= 23:
            base = resting + rng.uniform(-0.5, 2.5)
        elif hour < 8:
            base = resting + rng.uniform(2, 8)
        elif hour < 18:
            base = resting + rng.uniform(6, 22) + 4 * (hour - 12) ** 2 / 12
        else:
            base = resting + rng.uniform(4, 14)
        if exercise_at is not None and exercise_at <= slot < exercise_at + exercise_len:
            ramp = (slot - exercise_at) / max(1, exercise_len)
            base = 108 + 38 * min(1.0, ramp + 0.35) + rng.uniform(-4, 6)
        yield t, {"hr": round(min(191.0, max(36.0, base + rng.gauss(0, 1.6))), 1)}
        slot += 1
        t += INTRADAY_STEP


def seed_store(
    store: SeriesStore,
    *,
    days: int = 30,
    seed: int = 1337,
    end: date | None = None,
    drop_missing_days: bool = True,
) -> SeedPlan:
    """Fill ``store`` with ``days`` days of synthetic series ending at ``end``.

    ``drop_missing_days`` removes a few whole days so completeness reporting can
    be exercised; with it off the corpus is gapless.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    end_day = end or datetime.now(UTC).date()
    start_day = end_day - timedelta(days=days - 1)
    rng = random.Random(f"{seed}:{start_day.isoformat()}")
    per_dataset: dict[str, int] = {}
    dropped: list[date] = []
    total = 0
    rows: list[tuple[str, datetime, dict[str, float]]] = []

    for index, offset in enumerate(range(days)):
        day = start_day + timedelta(days=offset)
        if drop_missing_days and offset > 0 and rng.random() < 0.06:
            dropped.append(day)
            continue
        day_points = 0
        for ts, values in _intraday(day, index, rng):
            rows.append(("heart_rate", ts, values))
            day_points += 1
        resting = _resting_baseline(index) + rng.gauss(0, 1.1)
        rmssd = max(8.0, 118.0 - resting * 0.85 + rng.gauss(0, 6.0))
        rows.extend(
            [
                (
                    "daily_summary",
                    _day_start(day),
                    {
                        "resting_hr": round(resting, 1),
                        "steps": float(rng.randrange(3800, 14500)),
                        "calories": float(rng.randrange(1900, 3400)),
                        "active_minutes": float(rng.randrange(0, 95)),
                    },
                ),
                (
                    "hrv",
                    _day_start(day),
                    {
                        "rmssd": round(rmssd, 1),
                        "sdnn": round(rmssd * 1.35 + rng.gauss(0, 5), 1),
                        "hrv_n": float(rng.randrange(280, 420)),
                    },
                ),
                (
                    "sleep",
                    _day_start(day),
                    {
                        "sleep_minutes": float(rng.randrange(5 * 60, 8 * 60 + 40)),
                        "awake_minutes": float(rng.randrange(5, 70)),
                        "deep_minutes": float(rng.randrange(40, 130)),
                    },
                ),
                (
                    "spo2",
                    _day_start(day),
                    {
                        "spo2_min": round(rng.uniform(88.5, 95.5), 1),
                        "spo2_avg": round(rng.uniform(95.0, 98.4), 1),
                    },
                ),
            ]
        )
        day_points += 4
        total += day_points

    store.put_many(rows)
    for name in DATASETS:
        per_dataset[name] = sum(1 for row in rows if row[0] == name)
    return SeedPlan(
        days=days,
        start=start_day,
        end=end_day,
        seed=seed,
        points=total,
        dropped_days=tuple(dropped),
        per_dataset=per_dataset,
    )
