"""Deterministic synthetic data - ``fm dev seed``.

Three reasons this exists here rather than in a test folder: plugin authors need
sample data before they have real data, the benchmark job needs a fixed corpus,
and the plugin contract test suite (``fitbit_monitor.plugins.testing``) is built on
it so a third-party plugin can be tested the same way ours are.

It is *deterministic*: same ``seed``, same end date, same bytes in the store. That
is what makes a benchmark baseline meaningful and a flaky test visible -
tests/test_store.py hashes the rows rather than eyeballing them.

It writes **fake data about nobody**. Nothing here came from an account or a device, it
never leaves the machine, and the abnormal episodes it injects are recorded as synthetic
labels in ``dev_annotation`` so a detector test can never mistake one for a real
arrhythmia (see ``README.md`` and ``docs/data-policy.md``).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Iterator, Sequence

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

#: The labelled episode shapes the detector suite needs coverage of.
ANOMALY_KINDS: tuple[str, ...] = ("sustained_high", "sustained_low", "irregular")
#: One episode every this many days when the caller does not say how many.
ANOMALY_EVERY_DAYS = 14


@dataclass(frozen=True)
class Episode:
    """One injected abnormal episode, with the label that says it is synthetic.

    The window is exactly the span whose values were overwritten, so a detector test can
    treat "an alert inside this window" as a true positive without re-deriving the
    generator's internals.
    """

    kind: str
    dataset: str
    start: datetime
    end: datetime
    label: str
    mean_hr: float
    minutes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "dataset": self.dataset,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
            "mean_hr": self.mean_hr,
            "minutes": self.minutes,
        }


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
    episodes: tuple[Episode, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "days": self.days,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "seed": self.seed,
            "points": self.points,
            "dropped_days": [d.isoformat() for d in self.dropped_days],
            "per_dataset": dict(self.per_dataset),
            "episodes": [episode.as_dict() for episode in self.episodes],
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


# --- labelled abnormal episodes --------------------------------------------
#: ``kind -> (earliest slot, latest slot, shortest run, longest run)`` in 5-minute
#: slots. Sustained bradycardia is placed overnight because that is where a real one
#: shows up (and where an exercise bump cannot be mistaken for it).
_EPISODE_SHAPE: dict[str, tuple[int, int, int, int]] = {
    "sustained_high": (60, 150, 8, 18),
    "sustained_low": (3, 40, 9, 24),
    "irregular": (40, 160, 6, 14),
}


def _episode_values(kind: str, rng: random.Random, alternating: bool, resting: float) -> float:
    """One synthetic reading inside an episode window."""
    if kind == "sustained_high":
        return 133.0 + rng.uniform(0.0, 22.0)
    if kind == "sustained_low":
        return 40.0 + rng.uniform(0.0, 6.0)
    # irregular: the point is the scatter, not the level - alternating extremes with
    # noise is what an irregularity heuristic is supposed to notice.
    swing = resting - 14.0 if alternating else resting + 46.0
    return swing + rng.uniform(-11.0, 11.0)


def _inject_episode(
    points: list[tuple[datetime, dict[str, float]]], kind: str, rng: random.Random, resting: float
) -> Episode:
    """Overwrite one window of a day's intraday points and return its label."""
    first, last, shortest, longest = _EPISODE_SHAPE[kind]
    start = rng.randrange(first, last)
    length = rng.randrange(shortest, longest)
    start = min(start, max(0, len(points) - 2))
    stop = min(len(points), start + length)
    values: list[float] = []
    for offset, (ts, reading) in enumerate(points[start:stop]):
        value = round(min(191.0, max(36.0, _episode_values(kind, rng, offset % 2 == 0, resting))), 1)
        reading["hr"] = value
        values.append(value)
    mean = round(sum(values) / len(values), 1) if values else 0.0
    return Episode(
        kind=kind,
        dataset="heart_rate",
        start=points[start][0],
        end=points[stop - 1][0],
        label=kind,
        mean_hr=mean,
        minutes=(stop - start) * int(INTRADAY_STEP.total_seconds() // 60),
    )


def record_episodes(store: SeriesStore, episodes: Sequence[Episode], *, seed: int) -> int:
    """Write the episode labels to ``dev_annotation`` (idempotent per seed).

    The label lives next to the data on purpose: a detector test needs ground truth in
    the same database it reads, and ``INSERT OR IGNORE`` on the unique key means seeding
    the same corpus twice does not double the labels.
    """
    for episode in episodes:
        store.conn.execute(
            "INSERT OR IGNORE INTO dev_annotation (kind, dataset, start_ts, end_ts, label, seed, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                episode.kind,
                episode.dataset,
                episode.start.isoformat(),
                episode.end.isoformat(),
                episode.label,
                seed,
                json.dumps({"mean_hr": episode.mean_hr, "minutes": episode.minutes, "synthetic": True}, sort_keys=True),
            ),
        )
    store.conn.commit()
    return len(episodes)


def episode_annotations(
    conn, *, dataset: str | None = None, kind: str | None = None
) -> list[dict[str, object]]:
    """Every injected episode label, oldest first. Works on a read-only connection."""
    sql = "SELECT kind, dataset, start_ts, end_ts, label, seed, details FROM dev_annotation"
    where: list[str] = []
    params: list[object] = []
    if dataset:
        where.append("dataset = ?")
        params.append(dataset)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_ts, kind"
    out: list[dict[str, object]] = []
    for row in conn.execute(sql, params):
        record = dict(row)
        record["details"] = json.loads(record["details"] or "{}")
        out.append(record)
    return out


def _episode_count(days: int, requested: int | None) -> int:
    """How many episodes to inject: what was asked for, or one per fortnight."""
    if requested is not None:
        return max(0, requested)
    if days < ANOMALY_EVERY_DAYS:
        return 0
    return max(1, days // ANOMALY_EVERY_DAYS)


def seed_store(
    store: SeriesStore,
    *,
    days: int = 90,
    seed: int = 1337,
    end: date | None = None,
    drop_missing_days: bool = True,
    anomalies: int | None = None,
) -> SeedPlan:
    """Fill ``store`` with ``days`` days of synthetic series ending at ``end``.

    ``drop_missing_days`` removes a few whole days so completeness reporting can
    be exercised; with it off the corpus is gapless.

    ``anomalies`` is how many labelled abnormal episodes to inject (one of
    ``sustained_high`` / ``sustained_low`` / ``irregular``, rotating), spread evenly over
    the corpus and written to ``dev_annotation`` as well as into the heart-rate series.
    ``None`` means one every two weeks; ``0`` means a clean corpus.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    end_day = end or datetime.now(UTC).date()
    start_day = end_day - timedelta(days=days - 1)
    rng = random.Random(f"{seed}:{start_day.isoformat()}")
    wanted = _episode_count(days, anomalies)
    every = max(2, days // wanted) if wanted else 0
    # Start in the middle of the first interval, not at its end: the schedule slides
    # forward when a scheduled day is one of the deliberate gaps, and starting late is
    # how the last episode falls off the end of the corpus.
    next_episode = max(1, every // 2) if every else None
    per_dataset: dict[str, int] = {}
    dropped: list[date] = []
    episodes: list[Episode] = []
    total = 0
    rows: list[tuple[str, datetime, dict[str, float]]] = []

    for index, offset in enumerate(range(days)):
        day = start_day + timedelta(days=offset)
        if drop_missing_days and offset > 0 and rng.random() < 0.06:
            dropped.append(day)
            continue
        day_points = 0
        points = list(_intraday(day, index, rng))
        if next_episode is not None and offset >= next_episode and len(episodes) < wanted:
            kind = ANOMALY_KINDS[len(episodes) % len(ANOMALY_KINDS)]
            episodes.append(_inject_episode(points, kind, rng, _resting_baseline(index)))
            next_episode = offset + every
        for ts, values in points:
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

    store.put_many(rows, source_id="dev seed")
    if episodes:
        record_episodes(store, episodes, seed=seed)
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
        episodes=tuple(episodes),
    )
