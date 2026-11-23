"""Per-epoch heart rate: one row per epoch, with everything wrong with it kept in.

This is the bottom layer the rest of :mod:`fitbit_monitor.hr` is built on, and it is
built on the read API (:class:`fitbit_monitor.store.reader.ReadOnlyStore`) rather than
on SQL, so the store stays the only thing that knows how the rows were stored.

The one promise the whole module exists to keep
---------------------------------------------
**Nothing is silently dropped.** A day with holes in it, a sensor that reported ``0``
because it lost the pulse, and a payload that carried ``null`` all produce a *recorded
result* - a :class:`Sample` with a flag, or a :class:`~fitbit_monitor.store.reader.Gap`
in :attr:`EpochSeries.gaps` - and every count that a downstream threshold divides by
comes out of the same object. A rule that averages :attr:`EpochSeries.values` sees a
clean series; a rule that cares whether the series is trustworthy reads
:attr:`EpochSeries.impossible`, :attr:`EpochSeries.gaps` and
:attr:`EpochSeries.coverage` first. Both are the same object, so neither is a guess.

Three things can be wrong with a stored reading, and they are kept apart because they
want different treatment:

``absent``
    There is no reading at this epoch at all - it is inside a gap. Reported as a
    :class:`~fitbit_monitor.store.reader.Gap`; a value may be *imputed* across a short
    one (see ``impute_seconds``) and that value carries ``imputed``.
``out_of_range``
    A number, but not a plausible one: at or below zero (the device's "no signal"), or
    outside ``[min_plausible_bpm, max_plausible_bpm]`` - see :mod:`fitbit_monitor.hr.util` for why the band
    sits where it does. The raw value is kept on ``raw`` for reporting.
``not_a_number``
    ``null``, a string, ``nan``, ``inf`` - the payload parsed, the field did not.

Epochs and gaps
---------------
The **epoch** is the native spacing of the series: 60 seconds for ``heart_rate`` per the
store catalog (:func:`fitbit_monitor.store.datasets.spec`), which is what the read API's
own gap rule uses too. A gap is a break longer than ``tolerance`` times the grain, where
the grain is the coarser of the catalog's and the spacing actually present - the same
rule ``ReadOnlyStore.gaps`` applies, so this layer and the store cannot disagree about
whether a morning is "sparse" or "broken". Data imported at 5-minute spacing therefore
does not read as one long gap, and a watch that stopped for two hours does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from ..store.reader import Gap, ReadOnlyStore
from .util import (
    DEFAULT_MAX_BPM,
    DEFAULT_MIN_BPM,
    FLAG_ABSENT,
    FLAG_IMPUTED,
    FLAG_NOT_A_NUMBER,
    FLAG_OUT_OF_RANGE,
    UNIT_BPM,
    is_number,
    iso,
    mean,
    median,
    minutes,
)

#: Used when a dataset declares no native grain (the store uses the same fallback).
DEFAULT_EPOCH_SECONDS = 60.0
#: A break longer than this many epochs is a gap - the store's own tolerance.
DEFAULT_GAP_TOLERANCE = 3.0
#: Shortest gap the collector can produce; only used to size the default imputation.
_MIN_EPOCH_SECONDS = 1.0


@dataclass(frozen=True)
class Sample:
    """One epoch's reading - measured, imputed, or refused, and which it was.

    ``bpm`` is the value in :data:`~fitbit_monitor.hr.util.UNIT_BPM`, or ``None`` when
    there is nothing usable at this epoch. ``raw`` keeps what the store actually held,
    so a report can print "reading 255 at 03:12 was refused" instead of a missing row.
    """

    ts: datetime
    bpm: float | None = None
    #: What the store held at this instant: a number, or the junk that was not one.
    raw: object = None
    flags: tuple[str, ...] = ()
    epoch_seconds: float = DEFAULT_EPOCH_SECONDS

    # --- what it is ---------------------------------------------------------
    @property
    def measured(self) -> bool:
        """A reading the device actually took, and kept."""
        return self.bpm is not None and not self.flags

    @property
    def usable(self) -> bool:
        """May a feature read ``bpm`` - measured or honestly imputed."""
        return self.bpm is not None and set(self.flags) <= {FLAG_IMPUTED}

    @property
    def imputed(self) -> bool:
        return FLAG_IMPUTED in self.flags

    @property
    def refused(self) -> bool:
        """There is a row here and it was rejected; it is not the same as absent."""
        return bool(set(self.flags) & {FLAG_OUT_OF_RANGE, FLAG_NOT_A_NUMBER})

    @property
    def unit(self) -> str:
        return UNIT_BPM

    @property
    def reason(self) -> str | None:
        """Short human note for why there is no value, or ``None`` when there is one."""
        if self.measured:
            return None
        if self.imputed:
            return "interpolated across a short gap"
        if FLAG_NOT_A_NUMBER in self.flags:
            return f"stored value {self.raw!r} is not a number"
        if FLAG_OUT_OF_RANGE in self.flags:
            return f"stored value {self.raw!r} is outside the plausible band"
        return FLAG_ABSENT

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts.isoformat(),
            "bpm": None if self.bpm is None else round(float(self.bpm), 1),
            "raw": self.raw,
            "flags": list(self.flags),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EpochSeries:
    """The heart rate in one range, at its own grain, plus everything wrong with it.

    Built by :func:`epoch_series`. Iterate it for the samples; read the properties for
    the numbers a rule divides by. Values are ordered oldest-first and every instant is
    aware UTC.
    """

    since: datetime
    until: datetime
    samples: tuple[Sample, ...] = ()
    gaps: tuple[Gap, ...] = ()
    epoch_seconds: float = DEFAULT_EPOCH_SECONDS
    tolerance: float = DEFAULT_GAP_TOLERANCE
    dataset: str = "heart_rate"
    field: str = "hr"
    min_plausible_bpm: float = DEFAULT_MIN_BPM
    max_plausible_bpm: float = DEFAULT_MAX_BPM
    impute_seconds: float = 0.0
    observed_epoch_seconds: float | None = None

    # --- shape --------------------------------------------------------------
    @property
    def effective_epoch_seconds(self) -> float:
        """The spacing durations are computed with: the coarser of catalog grain and actual.

        A corpus stored at 5-minute spacing does not have 60-second epochs whatever the
        catalog says, and every "was this sustained for 8 minutes" and "grow this block by
        one epoch" calculation has to be measured in the spacing the data really has.
        Same rule the store's own gap finder uses, so the two cannot disagree.
        """
        return max(float(self.epoch_seconds), float(self.observed_epoch_seconds or 0.0))

    def __len__(self) -> int:
        return len(self.samples)

    def __iter__(self):
        return iter(self.samples)

    @property
    def has_data(self) -> bool:
        """At least one usable reading in the range."""
        return bool(self.values)

    @property
    def samples_usable(self) -> tuple[Sample, ...]:
        return tuple(sample for sample in self.samples if sample.usable)

    @property
    def measured_samples(self) -> tuple[Sample, ...]:
        return tuple(sample for sample in self.samples if sample.measured)

    @property
    def imputed_samples(self) -> tuple[Sample, ...]:
        return tuple(sample for sample in self.samples if sample.imputed)

    @property
    def impossible(self) -> tuple[Sample, ...]:
        """Every sample whose stored value was refused - the "not silently dropped" list."""
        return tuple(sample for sample in self.samples if sample.refused)

    @property
    def values(self) -> tuple[float, ...]:
        """Every usable rate in the range, oldest first (measured and imputed)."""
        return tuple(float(s.bpm) for s in self.samples if s.usable and s.bpm is not None)

    @property
    def measured_values(self) -> tuple[float, ...]:
        """Only what the device measured - what HRV and any "hard" evidence must use."""
        return tuple(float(s.bpm) for s in self.samples if s.measured and s.bpm is not None)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "samples": len(self.samples),
            "usable": len(self.values),
            "measured": len(self.measured_samples),
            "imputed": len(self.imputed_samples),
            "impossible": len(self.impossible),
            "not_a_number": sum(1 for s in self.samples if FLAG_NOT_A_NUMBER in s.flags),
            "out_of_range": sum(1 for s in self.samples if FLAG_OUT_OF_RANGE in s.flags),
            "gaps": len(self.gaps),
        }

    # --- coverage -----------------------------------------------------------
    @property
    def span_seconds(self) -> float:
        return (self.until - self.since).total_seconds()

    @property
    def observed_seconds(self) -> float:
        """Seconds covered by a usable reading, counted in the effective epoch.

        A series stored at 5-minute spacing covers 300 seconds per reading, not the 60
        the catalog claims; counting the catalog's epoch would report a complete day as
        20% coverage and make every "is this window trustworthy" rule meaningless.
        """
        return len(self.values) * self.effective_epoch_seconds

    @property
    def coverage(self) -> float:
        """Fraction of the range that has a usable reading, 0..1.

        Computed from the epoch count rather than wall-clock time, so a sparse import
        cannot report 100% coverage by having one reading at each end of a day.
        """
        if self.span_seconds <= 0:
            return 0.0
        return max(0.0, min(1.0, self.observed_seconds / self.span_seconds))

    @property
    def first_ts(self) -> datetime | None:
        return self.samples_usable[0].ts if self.samples_usable else None

    @property
    def last_ts(self) -> datetime | None:
        return self.samples_usable[-1].ts if self.samples_usable else None

    @property
    def longest_gap(self) -> Gap | None:
        return max(self.gaps, key=lambda gap: gap.seconds) if self.gaps else None

    @property
    def max_gap_seconds(self) -> float:
        gap = self.longest_gap
        return gap.seconds if gap else 0.0

    # --- statistics over the usable values ----------------------------------
    @property
    def mean_bpm(self) -> float | None:
        values = self.values
        return mean(values) if values else None

    @property
    def median_bpm(self) -> float | None:
        values = self.values
        return median(values) if values else None

    @property
    def min_bpm(self) -> float | None:
        values = self.values
        return min(values) if values else None

    @property
    def max_bpm(self) -> float | None:
        values = self.values
        return max(values) if values else None

    # --- sub-ranges ---------------------------------------------------------
    def samples_between(self, start: datetime, end: datetime) -> tuple[Sample, ...]:
        """The samples in the half-open ``[start, end)``."""
        return tuple(s for s in self.samples if start <= s.ts < end)

    def slice(self, start: datetime, end: datetime) -> "EpochSeries":
        """The same series restricted to ``[start, end)``, with its own gaps recomputed.

        Gaps are recomputed for the new bounds on purpose: a window that begins in the
        middle of a two-hour hole should say so, and that is exactly the mistake a
        day-by-day baseline makes when it cuts a range up and keeps the whole-range gaps.
        """
        chosen = self.samples_between(start, end)
        return EpochSeries(
            since=start,
            until=end,
            samples=chosen,
            gaps=find_gaps(
                chosen,
                epoch_seconds=self.epoch_seconds,
                tolerance=self.tolerance,
                since=start,
                until=end,
                observed_epoch_seconds=self.observed_epoch_seconds,
            ),
            epoch_seconds=self.epoch_seconds,
            tolerance=self.tolerance,
            dataset=self.dataset,
            field=self.field,
            min_plausible_bpm=self.min_plausible_bpm,
            max_plausible_bpm=self.max_plausible_bpm,
            impute_seconds=self.impute_seconds,
            observed_epoch_seconds=self.observed_epoch_seconds,
        )

    def between(self, start: datetime, end: datetime) -> tuple[float, ...]:
        """Usable rates in ``[start, end)``."""
        return tuple(
            float(s.bpm) for s in self.samples_between(start, end) if s.usable and s.bpm is not None
        )

    # --- reporting ----------------------------------------------------------
    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "field": self.field,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "epoch_seconds": self.epoch_seconds,
            "observed_epoch_seconds": self.observed_epoch_seconds,
            "tolerance": self.tolerance,
            "plausible_band_bpm": [self.min_plausible_bpm, self.max_plausible_bpm],
            "unit": UNIT_BPM,
            "impute_seconds": self.impute_seconds,
            **self.counts,
            "coverage": round(self.coverage, 4),
            "observed_minutes": minutes(self.observed_seconds),
            "span_minutes": minutes(self.span_seconds),
            "first_ts": iso(self.first_ts),
            "last_ts": iso(self.last_ts),
            "mean_bpm": None if self.mean_bpm is None else round(self.mean_bpm, 1),
            "median_bpm": None if self.median_bpm is None else round(self.median_bpm, 1),
            "min_bpm": None if self.min_bpm is None else round(self.min_bpm, 1),
            "max_bpm": None if self.max_bpm is None else round(self.max_bpm, 1),
            "max_gap_seconds": round(self.max_gap_seconds, 1),
            "gap_details": [gap.as_dict() for gap in self.gaps],
            "impossible": [sample.as_dict() for sample in self.impossible],
        }


def build_samples(
    rows: Iterable[tuple[datetime, float | None]],
    *,
    epoch_seconds: float,
    min_plausible_bpm: float = DEFAULT_MIN_BPM,
    max_plausible_bpm: float = DEFAULT_MAX_BPM,
    impute_seconds: float = 0.0,
) -> tuple[Sample, ...]:
    """Turn ``(instant, stored value)`` pairs into samples: classify, then impute.

    Takes pairs rather than a store so a test can hand it a hand-built series - which is
    how every numeric assertion in this package is written - while
    :func:`epoch_series` stays a thin read of the store.
    """
    samples: list[Sample] = []
    for ts, raw in rows:
        samples.append(
            classify(
                ts,
                raw,
                epoch_seconds=epoch_seconds,
                min_plausible_bpm=min_plausible_bpm,
                max_plausible_bpm=max_plausible_bpm,
            )
        )
    if impute_seconds > 0:
        samples = _impute(samples, epoch_seconds=epoch_seconds, impute_seconds=impute_seconds)
    return tuple(samples)


def classify(
    ts: datetime,
    raw: object,
    *,
    epoch_seconds: float,
    min_plausible_bpm: float = DEFAULT_MIN_BPM,
    max_plausible_bpm: float = DEFAULT_MAX_BPM,
) -> Sample:
    """One stored value, judged. Never raises: a bad value is a flag, not an error."""
    if not is_number(raw):
        return Sample(ts=ts, bpm=None, raw=raw, flags=(FLAG_NOT_A_NUMBER,), epoch_seconds=epoch_seconds)
    value = float(raw)  # type: ignore[arg-type]
    if not min_plausible_bpm <= value <= max_plausible_bpm:
        return Sample(ts=ts, bpm=None, raw=value, flags=(FLAG_OUT_OF_RANGE,), epoch_seconds=epoch_seconds)
    return Sample(ts=ts, bpm=value, raw=value, flags=(), epoch_seconds=epoch_seconds)


def _impute(samples: list[Sample], *, epoch_seconds: float, impute_seconds: float) -> list[Sample]:
    """Fill short holes between two usable readings with straight-line values.

    Only ever between two usable readings, never across a refused value and never across
    a gap longer than ``impute_seconds`` - a filled row is a guess, so the number of
    guesses is capped by the caller and every one of them is flagged ``imputed`` and kept
    out of :attr:`EpochSeries.measured_values`.

    The spacing filled is the one passed in, which is the series' *effective* epoch: a
    corpus stored five minutes apart is not retrofitted with invented minutes.
    """
    usable = [s for s in samples if s.usable]
    if len(usable) < 2:
        return samples
    filled: list[Sample] = []
    previous: Sample | None = None
    for sample in samples:
        # `previous` is the row immediately before this one, so a refused reading in
        # between breaks the pair: a hole is only ever filled between two neighbours.
        if previous is not None and previous.usable and sample.usable:
            span = (sample.ts - previous.ts).total_seconds()
            steps = int(round(span / max(_MIN_EPOCH_SECONDS, epoch_seconds))) - 1
            if 0 < steps and span <= impute_seconds + epoch_seconds:
                low, high = float(previous.bpm), float(sample.bpm)  # type: ignore[arg-type]
                for step in range(1, steps + 1):
                    fraction = step / (steps + 1)
                    filled.append(
                        Sample(
                            ts=previous.ts + timedelta(seconds=epoch_seconds * step),
                            bpm=low + (high - low) * fraction,
                            raw=None,
                            flags=(FLAG_IMPUTED,),
                            epoch_seconds=epoch_seconds,
                        )
                    )
        filled.append(sample)
        previous = sample
    return filled

def grain_seconds(samples: Sequence[Sample], *, minimum: int = 8) -> float | None:
    """The spacing actually present: the lower quartile of consecutive differences.

    Same rule as the store's ``_observed_grain_seconds``: the *lower* quartile, because
    what is being looked for is the ordinary spacing, and a median walks away from the
    truth as soon as the short sample contains the very gap being measured. ``None``
    when there are too few spacings to say.
    """
    stamps = [sample.ts for sample in samples if sample.usable]
    if len(stamps) < minimum + 1:
        return None
    diffs = sorted((b - a).total_seconds() for a, b in zip(stamps, stamps[1:]))
    if not diffs:  # pragma: no cover - empty by construction above
        return None
    return float(diffs[len(diffs) // 4])


def find_gaps(
    samples: Sequence[Sample],
    *,
    epoch_seconds: float,
    tolerance: float = DEFAULT_GAP_TOLERANCE,
    since: datetime | None = None,
    until: datetime | None = None,
    observed_epoch_seconds: float | None = None,
    edges: bool = True,
) -> tuple[Gap, ...]:
    """Breaks in ``samples``, longest first - interior breaks and, when asked, the edges.

    The grain used is ``max(epoch_seconds, observed)`` so a series stored sparser than
    the catalog claims is not "all gaps", and the tolerance is the store's own. Edge
    gaps matter as much as interior ones: a day whose first reading is at 11:00 is a
    morning of missing data, and a rule that only looks between the first and last
    reading would never see it.
    """
    usable = [s for s in samples if s.usable]
    step = max(float(epoch_seconds), float(observed_epoch_seconds or 0.0))
    limit = step * max(1.0, tolerance)
    if not usable:
        if since is not None and until is not None and until > since:
            return (Gap(start=since, end=until, seconds=(until - since).total_seconds()),)
        return ()
    found: list[Gap] = []
    for before, after in zip(usable, usable[1:]):
        seconds = (after.ts - before.ts).total_seconds()
        if seconds > limit:
            found.append(Gap(start=before.ts, end=after.ts, seconds=seconds))
    if edges and since is not None:
        lead = (usable[0].ts - since).total_seconds()
        if lead > limit:
            found.append(Gap(start=since, end=usable[0].ts, seconds=lead))
    if edges and until is not None:
        trail = (until - usable[-1].ts).total_seconds()
        if trail > limit:
            found.append(Gap(start=usable[-1].ts, end=until, seconds=trail))
    return tuple(sorted(found, key=lambda gap: -gap.seconds))


def epoch_series(
    store: ReadOnlyStore,
    since: datetime,
    until: datetime,
    *,
    epoch_seconds: float | None = None,
    tolerance: float = DEFAULT_GAP_TOLERANCE,
    impute_seconds: float | None = None,
    min_plausible_bpm: float = DEFAULT_MIN_BPM,
    max_plausible_bpm: float = DEFAULT_MAX_BPM,
    dataset: str = "heart_rate",
    field: str = "hr",
) -> EpochSeries:
    """Read one range of intraday heart rate as an :class:`EpochSeries`.

    ``store`` is the read API and is only ever read from. The range is half-open
    ``[since, until)`` and both ends must be timezone-aware (the store refuses naive
    instants, and so does this).

    Defaults worth knowing, because a downstream threshold depends on each:

    * ``epoch_seconds`` - the dataset's catalog grain (60 s for ``heart_rate``).
    * ``tolerance`` - 3 epochs, the store's own :data:`~fitbit_monitor.store.reader.GAP_TOLERANCE`.
    * ``impute_seconds`` - twice the effective epoch, the spacing the data actually has:
      at most two readings are ever invented between two real ones, and a 5-minute corpus
      is not retrofitted with invented minutes. Pass ``0`` for a series nobody can mistake
      for complete.
    * ``min_plausible_bpm`` / ``max_plausible_bpm`` - the plausibility band in :mod:`fitbit_monitor.hr.util`.
    """
    if epoch_seconds is None:
        epoch_seconds = float(store.grain_seconds(dataset) or DEFAULT_EPOCH_SECONDS)
    epoch_seconds = max(_MIN_EPOCH_SECONDS, float(epoch_seconds))
    if until <= since:
        raise ValueError(f"epoch_series range is empty or inverted: {iso(since)} .. {iso(until)}")

    # The store upserts by (dataset, instant), so one instant is one row: a range imported
    # twice cannot double a minute.
    rows: list[tuple[datetime, float | None]] = sorted(
        ((point.ts, point.values.get(field)) for point in store.query(dataset, since, until)),
        key=lambda row: row[0],
    )
    samples: tuple[Sample, ...] = build_samples(
        rows,
        epoch_seconds=epoch_seconds,
        min_plausible_bpm=min_plausible_bpm,
        max_plausible_bpm=max_plausible_bpm,
        impute_seconds=0.0,
    )
    # The grain the data actually has is measured *before* anything is invented, so that
    # imputation fills holes at the ordinary spacing instead of retrofitting a catalog
    # spacing the corpus never had.
    observed = grain_seconds(samples)
    effective = max(epoch_seconds, float(observed or 0.0))
    if impute_seconds is None:
        impute_seconds = 2 * effective
    if impute_seconds > 0:
        samples = tuple(
            _impute(list(samples), epoch_seconds=effective, impute_seconds=float(impute_seconds))
        )
    gaps = find_gaps(
        samples,
        epoch_seconds=epoch_seconds,
        tolerance=tolerance,
        since=since,
        until=until,
        observed_epoch_seconds=observed,
    )
    return EpochSeries(
        since=since,
        until=until,
        samples=samples,
        gaps=gaps,
        epoch_seconds=epoch_seconds,
        tolerance=tolerance,
        dataset=dataset,
        field=field,
        min_plausible_bpm=min_plausible_bpm,
        max_plausible_bpm=max_plausible_bpm,
        impute_seconds=float(impute_seconds),
        observed_epoch_seconds=observed,
    )


__all__ = [
    "DEFAULT_EPOCH_SECONDS",
    "DEFAULT_GAP_TOLERANCE",
    "EpochSeries",
    "Sample",
    "build_samples",
    "classify",
    "epoch_series",
    "find_gaps",
    "grain_seconds",
]
