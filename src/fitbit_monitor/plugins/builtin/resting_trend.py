"""``resting_trend`` - the reference METRIC plugin.

Weekly resting heart rate and HRV, the two numbers most people actually track, with
week-over-week and 4-week deltas and honest completeness reporting. It emits metrics and
no alerts on purpose: it exists to be the readable example in ``docs/plugins.md``, and
:mod:`hr_anomaly` beside it is the alerting example.

It also demonstrates the four things every real plugin needs:

* **declared options** with types, defaults and help text (``fm plugins show``),
* **downsampling** - intraday heart rate is 30k points a week, and this plugin never
  holds them all,
* **honesty about what is missing** - a week with two days of data says so, and carries
  that fraction in ``confidence`` so a chart can show it; a week with no data at all is
  named in ``resting_trend_data_coverage`` instead of going quietly missing from the
  output; and the HRV measure reported is whichever of ``rmssd`` / ``sdnn`` the store
  actually carries, named in every metric that uses it,
* **a checkpoint** - ``ctx.check_timeout()`` inside the long loops, because the
  wall-clock budget belongs to the host.

Run it::

    fm plugins run resting_trend --since 8w
    fm plugins run resting_trend --since 8w --json
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Iterable

from ..contract import MonitorPlugin, PluginContext, PluginOption
from ...store.downsample import daily_values
from ...timeutil import DateRange

DAY = timedelta(days=1)

#: The HRV measures this plugin can report, in the order ``hrv_field = "auto"`` tries them.
HRV_FIELDS = ("rmssd", "sdnn")

#: Intraday points read between two cooperative timeout checks.
CHECK_EVERY_POINTS = 4096


class RestingTrendPlugin(MonitorPlugin):
    """Weekly resting-HR and HRV trend with deltas and completeness reporting."""

    name = "resting_trend"
    summary = "weekly resting heart rate and HRV trend, with WoW and 4-week deltas"
    version = "0.1.0"
    datasets = ("daily_summary", "hrv", "heart_rate")
    options = (
        PluginOption("lookback_weeks", "int", 8, "number of ISO weeks to report"),
        PluginOption(
            "min_days_per_week",
            "int",
            4,
            "weeks with fewer days present than this are reported as incomplete",
        ),
        PluginOption(
            "hrv_field",
            "str",
            "auto",
            "which HRV measure to report; auto uses whichever this store actually has, rmssd first",
            choices=("auto", *HRV_FIELDS),
        ),
        PluginOption(
            "resting_source",
            "str",
            "auto",
            "where resting HR comes from: a stored daily value, or the quiet overnight window",
            choices=("auto", "daily_summary", "heart_rate"),
        ),
    )

    # ------------------------------------------------------------------
    def configure(self, config) -> None:
        super().configure(config)
        self.lookback_weeks = max(1, int(config.get_int("lookback_weeks", 8)))
        self.min_days = max(1, int(config.get_int("min_days_per_week", 4)))
        self.hrv_field = config.get_str("hrv_field", "auto")
        self.resting_source = config.get_str("resting_source", "auto")
        if self.hrv_field not in ("auto", *HRV_FIELDS):
            raise ValueError(f"hrv_field must be auto, rmssd or sdnn, got {self.hrv_field!r}")
        if self.resting_source not in ("auto", "daily_summary", "heart_rate"):
            raise ValueError(
                f"resting_source must be auto, daily_summary or heart_rate, got {self.resting_source!r}"
            )

    # ------------------------------------------------------------------
    def run(self, ctx: PluginContext) -> Iterable[Any] | None:
        last_day = self._last_expected_day(ctx.window)
        starts = self._week_starts(last_day)
        window = self._analysis_window(ctx.window, starts[0])
        resting = self._resting_by_day(ctx, window)
        hrv = self._hrv_by_day(ctx, window)
        weeks = [
            WeekStats.build(start, min(start + 6 * DAY, last_day), resting, hrv.values)
            for start in starts
        ]
        reported = weeks[self.lookback_week_report_offset() :]
        # Coverage first: it is the sentence the rest of the run has to be read against,
        # and it is the only line that survives when there is nothing else to say.
        self._emit_coverage(ctx, reported, hrv)
        if not resting and not hrv.values:
            # Not an error: the caller already decided there is *some* data in range.
            ctx.log.info("no resting-HR or HRV values in %s: %s", window, hrv.note or "")
            return None
        for stats in reported:
            ctx.check_timeout()
            self._emit(ctx, stats, weeks, hrv)
        return None

    # --- window and week math ------------------------------------------------
    def lookback_week_report_offset(self) -> int:
        """Weeks computed but not reported - they only feed the 4-week delta."""
        return 4

    def _analysis_window(self, window: DateRange, earliest_week: date) -> DateRange:
        """The run window, widened back to the first week the report and its deltas need."""
        earliest = datetime(earliest_week.year, earliest_week.month, earliest_week.day, tzinfo=UTC)
        since = min(window.since, earliest)
        return DateRange(since=since, until=window.until)

    def _week_starts(self, last_day: date) -> list[date]:
        """Oldest-first ISO weeks, ending with the one holding ``last_day``."""
        last = week_start(last_day)
        total = self.lookback_weeks + self.lookback_week_report_offset()
        return [last - timedelta(days=7 * i) for i in range(total - 1, -1, -1)]

    @staticmethod
    def _last_expected_day(window: DateRange) -> date:
        """The last calendar day the window actually covers.

        The window is half-open, so a ``--until 2026-06-01`` run does not owe the reader a
        day of 2026-06-01, and pretending it does would report a missing day nobody asked
        for. A window ending mid-day does owe today, because today is what it is.
        """
        until = ctx_day(window.until)
        return until - DAY if window.until.time() == time.min else until

    # --- data -----------------------------------------------------------------
    def _resting_by_day(self, ctx: PluginContext, window: DateRange) -> dict[date, float]:
        """Daily resting HR, from the stored daily value or the overnight quiet window."""
        preferred = self.resting_source
        if preferred in ("auto", "daily_summary"):
            stored = daily_values(
                ctx.store.query("daily_summary", window.since, window.until, fields=["resting_hr"]),
                "resting_hr",
            )
            if stored or preferred == "daily_summary":
                return stored
            ctx.log.info("daily_summary.resting_hr absent for range; falling back to the overnight window")
        return self._overnight_resting(ctx, window)

    def _overnight_resting(self, ctx: PluginContext, window: DateRange) -> dict[date, float]:
        """Mean heart rate over 00:00-05:55 UTC per day, from intraday ``heart_rate``.

        A stand-in for a proper resting-window calculation (that lives in the
        heart-rate epic): it shows a plugin doing its own windowed aggregation with
        ``downsample`` rather than reading a pre-computed daily field.
        """
        overnight: dict[date, list[float]] = {}
        seen = 0
        for point in ctx.store.query("heart_rate", window.since, window.until, fields=["hr"]):
            seen += 1
            if seen % CHECK_EVERY_POINTS == 0:
                ctx.check_timeout()
            if point.ts.hour < 6:
                overnight.setdefault(point.ts.date(), []).append(point.values["hr"])
        return {day: mean(values) for day, values in sorted(overnight.items()) if values}

    def _hrv_by_day(self, ctx: PluginContext, window: DateRange) -> "HrvMeasure":
        """The HRV series to report, and in prose why that one is what the store supports.

        ``hrv_field = "auto"`` is the default because a Fitbit Takeout archive carries
        RMSSD and nothing else while other sources publish SDNN, and a weekly trend that
        silently reports nothing because it asked for the wrong column is worse than one
        that picks the column that exists and says which it picked.
        """
        requested = self.hrv_field
        candidates = list(HRV_FIELDS) if requested == "auto" else [requested]
        tried: list[str] = []
        for name in candidates:
            values = daily_values(
                ctx.store.query("hrv", window.since, window.until, fields=[name]), name
            )
            if values:
                # `tried` is non-empty only when auto stepped past a measure the store
                # does not carry - which the reader is owed, because it is a fact about
                # their data and not about their config.
                note = (
                    f"{', '.join(tried)} has no values in this range; reporting {name}, which does"
                    if tried
                    else None
                )
                if note:
                    ctx.log.info("%s", note)
                return HrvMeasure(requested=requested, field=name, values=values, note=note)
            tried.append(name)
        note = (
            f"no hrv rows carrying {' or '.join(tried)} in this range"
            if requested == "auto"
            else f"hrv_field={requested} has no values in this range; no HRV metric is reported"
        )
        ctx.log.info("%s", note)
        return HrvMeasure(requested=requested, field=None, values={}, note=note)

    # --- emission -------------------------------------------------------------
    def _emit_coverage(
        self, ctx: PluginContext, reported: list["WeekStats"], hrv: "HrvMeasure"
    ) -> None:
        """How much of what was asked for was there - said before any weekly number is.

        ``reported`` is never empty: ``lookback_weeks`` is floored at 1 and four delta
        weeks are always computed beside it.
        """
        with_data = [s for s in reported if s.has_data]
        missing = [s.iso_week for s in reported if not s.has_data]
        incomplete = [s.iso_week for s in reported if s.has_data and s.days_present < self.min_days]
        fraction = len(with_data) / len(reported)
        details: dict[str, Any] = {
            "weeks_requested": len(reported),
            "weeks_with_data": len(with_data),
            "weeks_without_data": missing,
            "weeks_incomplete": incomplete,
            "min_days_per_week": self.min_days,
            "hrv_field_requested": hrv.requested,
            "hrv_field_used": hrv.field or "none",
            "hrv_days_present": len(hrv.values),
        }
        if hrv.note:
            details["note"] = hrv.note
        summary = f"{len(with_data)} of {len(reported)} requested weeks had any data"
        if missing:
            summary += f"; none at all for {', '.join(missing)}"
        if incomplete:
            summary += f"; below {self.min_days} days for {', '.join(incomplete)}"
        summary += f"; HRV measure reported: {hrv.measure}"
        ctx.metric(
            "resting_trend_data_coverage",
            round(fraction, 4),
            unit="fraction of weeks",
            confidence=round(fraction, 4),
            labels={"hrv_measure": hrv.field or "none"},
            details=details,
            summary=summary,
        )

    def _emit(
        self,
        ctx: PluginContext,
        stats: "WeekStats",
        all_weeks: list["WeekStats"],
        hrv: "HrvMeasure",
    ) -> None:
        previous = _before(all_weeks, stats.week_start, 1)
        four_back = _before(all_weeks, stats.week_start, 4)
        complete = stats.days_present >= self.min_days
        base_details: dict[str, Any] = {
            "iso_week": stats.iso_week,
            "days_present": stats.days_present,
            "days_expected": stats.days_expected,
            "missing_days": [d.isoformat() for d in stats.missing_days],
            "complete": complete,
            "wow_delta": _delta(stats.resting_hr, previous.resting_hr if previous else None),
            "delta_4w": _delta(stats.resting_hr, four_back.resting_hr if four_back else None),
        }
        if stats.resting_hr is not None:
            ctx.metric(
                "resting_hr_weekly",
                round(stats.resting_hr, 2),
                unit="bpm",
                confidence=stats.confidence,
                window_start=_at(stats.week_start),
                window_end=_at(stats.week_start) + timedelta(days=7),
                labels={"iso_week": stats.iso_week, "complete": "yes" if complete else "no"},
                details=base_details,
                summary=(
                    f"{stats.iso_week}: {stats.resting_hr:.1f} bpm resting over "
                    f"{stats.days_present}/{stats.days_expected} days"
                ),
            )
        if stats.hrv is not None and hrv.field:
            hrv_details = dict(base_details)
            hrv_details.update(
                {
                    "hrv_field": hrv.field,
                    "hrv_field_requested": hrv.requested,
                    "hrv_days_present": stats.hrv_days_present,
                    "hrv_wow_delta": _delta(stats.hrv, previous.hrv if previous else None),
                    "hrv_delta_4w": _delta(stats.hrv, four_back.hrv if four_back else None),
                }
            )
            ctx.metric(
                "hrv_weekly",
                round(stats.hrv, 2),
                unit="ms",
                confidence=stats.confidence,
                window_start=_at(stats.week_start),
                window_end=_at(stats.week_start) + timedelta(days=7),
                labels={
                    "iso_week": stats.iso_week,
                    "hrv_measure": hrv.field,
                    "complete": "yes" if complete else "no",
                },
                details=hrv_details,
                summary=(
                    f"{stats.iso_week}: {hrv.field.upper()} {stats.hrv:.1f} ms over "
                    f"{stats.hrv_days_present}/{stats.days_expected} days"
                ),
            )


@dataclass(frozen=True)
class HrvMeasure:
    """Which HRV column this run reported, why that one, and the days it came from."""

    requested: str
    field: str | None
    values: dict[date, float]
    note: str | None = None

    @property
    def measure(self) -> str:
        return self.field.upper() if self.field else "none"


class WeekStats:
    """One ISO week's aggregates. Named so the metric code reads as prose."""

    def __init__(
        self,
        week_start: date,
        last_day: date,
        resting_hr: float | None,
        hrv: float | None,
        days_present: int,
        days_expected: int,
        missing_days: tuple[date, ...],
        hrv_days_present: int,
    ) -> None:
        self.week_start = week_start
        self.last_day = last_day
        self.resting_hr = resting_hr
        self.hrv = hrv
        self.days_present = days_present
        self.days_expected = days_expected
        self.missing_days = missing_days
        self.hrv_days_present = hrv_days_present

    @property
    def iso_week(self) -> str:
        year, week, _ = self.week_start.isocalendar()
        return f"{year}-W{week:02d}"

    @property
    def has_data(self) -> bool:
        """Any of the two measures made it into this week."""
        return bool(self.days_present or self.hrv_days_present)

    @property
    def confidence(self) -> float:
        """Fraction of the week actually observed.

        ``days_expected`` is never 0 - a week is only built when at least its Monday has
        started inside the window - and ``days_present`` can never exceed it, so this is
        always a fraction in 0..1 without any clamping.
        """
        return self.days_present / self.days_expected

    @classmethod
    def build(
        cls,
        week_start: date,
        last_day: date,
        resting: dict[date, float],
        hrv: dict[date, float],
    ) -> "WeekStats":
        days = [week_start + timedelta(days=i) for i in range(7)]
        days = [d for d in days if d <= last_day]
        resting_present = [d for d in days if d in resting]
        hrv_present = [d for d in days if d in hrv]
        return cls(
            week_start=week_start,
            last_day=last_day,
            resting_hr=mean([resting[d] for d in resting_present]) if resting_present else None,
            hrv=mean([hrv[d] for d in hrv_present]) if hrv_present else None,
            days_present=len(resting_present),
            days_expected=len(days),
            missing_days=tuple(d for d in days if d not in resting),
            hrv_days_present=len(hrv_present),
        )


# --- small helpers (module-level so tests can use them) ----------------------
def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def ctx_day(value: datetime) -> date:
    return value.astimezone(UTC).date()


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _before(weeks: list[WeekStats], week: date, back: int) -> WeekStats | None:
    target = week - timedelta(days=7 * back)
    for stats in weeks:
        if stats.week_start == target:
            return stats
    return None


def _delta(current: float | None, reference: float | None) -> float | None:
    if current is None or reference is None:
        return None
    return round(current - reference, 2)


__all__ = ["HrvMeasure", "RestingTrendPlugin", "WeekStats", "week_start"]
