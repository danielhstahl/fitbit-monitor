"""``resting_trend`` - the reference METRIC plugin.

Weekly resting heart rate and HRV, the two numbers most people actually track,
with week-over-week and 4-week deltas and honest completeness reporting. It emits
metrics and no alerts on purpose: it exists to be the readable example in
``docs/plugins.md``, and :mod:`hr_anomaly` beside it is the alerting example.

It also demonstrates the three things every real plugin needs:

* **declared options** with types, defaults and help text (``fm plugins show``),
* **downsampling** - intraday heart rate is 30k points a week, and this plugin
  never holds them all, and
* **completeness honesty** - a week with two days of data is reported as such,
  with ``confidence`` carrying that fraction so a chart can show it.

Run it::

    fm plugins run resting_trend --since 8w
    fm plugins run resting_trend --since 8w --json
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable

from ..contract import MonitorPlugin, PluginContext, PluginOption
from ...store.downsample import daily_values
from ...timeutil import DateRange

DAY = timedelta(days=1)


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
            "rmssd",
            "which HRV measure to report",
            choices=("rmssd", "sdnn"),
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
        self.hrv_field = config.get_str("hrv_field", "rmssd")
        self.resting_source = config.get_str("resting_source", "auto")
        if self.hrv_field not in ("rmssd", "sdnn"):
            raise ValueError(f"hrv_field must be rmssd or sdnn, got {self.hrv_field!r}")
        if self.resting_source not in ("auto", "daily_summary", "heart_rate"):
            raise ValueError(f"resting_source must be auto, daily_summary or heart_rate, got {self.resting_source!r}")

    # ------------------------------------------------------------------
    def run(self, ctx: PluginContext) -> Iterable[Any] | None:
        window = self._analysis_window(ctx.window)
        resting = self._resting_by_day(ctx, window)
        hrv = daily_values(ctx.store.query("hrv", window.since, window.until), self.hrv_field)
        if not resting and not hrv:
            # Not an error: the caller already decided there is *some* data in range.
            ctx.log.info("no resting-HR or HRV values in %s", window)
            return None
        weeks = [
            WeekStats.build(week_start, min(week_start + 6 * DAY, window.until.date()), resting, hrv, self.hrv_field)
            for week_start in self._week_starts(window)
        ]
        reported = weeks[self.lookback_week_report_offset() :]
        for stats in reported:
            ctx.check_timeout()
            self._emit(ctx, stats, weeks)
        return None

    # --- window and week math ------------------------------------------------
    def lookback_week_report_offset(self) -> int:
        """Weeks computed but not reported - they only feed the 4-week delta."""
        return 4

    def _analysis_window(self, window: DateRange) -> DateRange:
        """The run window, widened back far enough to cover the lookback plus deltas."""
        weeks_needed = self.lookback_weeks + self.lookback_week_report_offset()
        earliest = week_start(ctx_day(window.until)) - timedelta(days=7 * weeks_needed)
        since = min(window.since, datetime(earliest.year, earliest.month, earliest.day, tzinfo=UTC))
        return DateRange(since=since, until=window.until)

    def _week_starts(self, window: DateRange) -> list[date]:
        last = week_start(ctx_day(window.until))
        total = self.lookback_weeks + self.lookback_week_report_offset()
        return [last - timedelta(days=7 * i) for i in range(total - 1, -1, -1)]

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
        for point in ctx.store.query("heart_rate", window.since, window.until, fields=["hr"]):
            if point.ts.hour < 6:
                overnight.setdefault(point.ts.date(), []).append(point.values["hr"])
        return {day: sum(values) / len(values) for day, values in sorted(overnight.items()) if values}

    # --- emission -------------------------------------------------------------
    def _emit(self, ctx: PluginContext, stats: "WeekStats", all_weeks: list["WeekStats"]) -> None:
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
                summary=f"{stats.resting_hr:.1f} bpm over {stats.days_present}/{stats.days_expected} days",
            )
        if stats.hrv is not None:
            hrv_details = dict(base_details)
            hrv_details.update(
                {
                    "hrv_field": self.hrv_field,
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
                    "hrv_measure": self.hrv_field,
                    "complete": "yes" if complete else "no",
                },
                details=hrv_details,
                summary=(
                    f"{self.hrv_field.upper()} {stats.hrv:.1f} ms over "
                    f"{stats.hrv_days_present}/{stats.days_expected} days"
                ),
            )


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
    def confidence(self) -> float:
        """Fraction of the week actually observed - never more than 1.0."""
        if self.days_expected <= 0:
            return 0.0
        return max(0.0, min(1.0, self.days_present / self.days_expected))

    @classmethod
    def build(
        cls,
        week_start: date,
        last_day: date,
        resting: dict[date, float],
        hrv: dict[date, float],
        hrv_field: str,
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


__all__ = ["RestingTrendPlugin", "WeekStats", "week_start"]
