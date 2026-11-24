"""The dataset catalog: what to ask the Google Health API for, and how to read the answer.

One :class:`Dataset` per importable dataset. Each one knows four things, and that is
the whole contract:

* which **scope** it needs - that is how ``fm import`` with no ``--dataset`` resolves
  "everything the account allows" (§4 of ADR-0002: four ``googlehealth.*`` scopes, no
  more and no less);
* how the API **addresses** the data type: a ``list`` of raw points for a *daily* type
  (already one row a day, so nothing to aggregate), a ``rollUp`` for a sub-daily type,
  a ``dailyRollUp`` for a daily view of an interval type;
* the **requests** for a date window, in order - including how many *pages* the API
  is expected to answer, because a page cap is a per-request fact and an import cost
  is a per-window one;
* how to **normalise** the responses into ``(timestamp, {field: value})`` rows.

Anything that is true for every dataset - chunking a window, counting requests,
following ``nextPageToken``, storing payloads, upserting rows, advancing watermarks -
lives in :mod:`fitbit_monitor.ingest.importer`, so adding a dataset means adding 30
lines here and a fixture, not touching the importer.

Three things about the v4 wire format are easy to get wrong and are pinned down in
:mod:`fitbit_monitor.api.health` rather than repeated here: the data type is
*kebab-case* in a path and *snake_case* in a filter; ``rollUp`` is a POST with a JSON
body while ``list`` is a GET with a filter string; and every page cap in here is
explicit, because the API's own default for a rollup is 1 440 points, not the 10 000
it documents as the maximum.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..errors import UsageError
from ..store.seed import DATASETS as SEED_DATASETS
from ..timeutil import parse_timestamp

#: Fields each dataset is expected to carry, for reporting and for ``fm dev seed`` parity.
FIELDS: dict[str, tuple[str, ...]] = dict(SEED_DATASETS)

# --- scopes (ADR-0002 §4) --------------------------------------------------
#: Every Google Health scope shares this prefix, and every one of them is Restricted.
SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth."

#: Heart rate, HRV, SpO2, respiratory rate, weight, glucose - the vitals.
HEALTH_METRICS_SCOPE = SCOPE_PREFIX + "health_metrics_and_measurements.readonly"
#: Steps, distance, floors, active minutes, calories, VO2 max - the activity side.
ACTIVITY_SCOPE = SCOPE_PREFIX + "activity_and_fitness.readonly"
#: Sleep sessions, stages and awakenings.
SLEEP_SCOPE = SCOPE_PREFIX + "sleep.readonly"
#: ``users/getProfile`` (age, member-since). Identity works on any grant, deliberately.
PROFILE_SCOPE = SCOPE_PREFIX + "profile.readonly"

#: The four scopes ``fm auth login`` asks for. Not ``.ecg``, not ``.irn``, not any
#: ``.writeonly``: a scope is a promise about data this tool actually reads, and it
#: never writes health data (ADR-0002 §4, "deliberately not requested").
ALL_SCOPES: tuple[str, ...] = (
    HEALTH_METRICS_SCOPE,
    ACTIVITY_SCOPE,
    SLEEP_SCOPE,
    PROFILE_SCOPE,
)

#: ``.writeonly`` scopes, named only so a config that lists one gets told why.
WRITE_SCOPES_HINT = "this tool never writes health data; remove the .writeonly scope"

# --- the shapes of the API (ADR-0002 §6, §7) -------------------------------
#: The resource root every data-point request hangs off. ``me`` is the authenticated
#: user; :func:`fitbit_monitor.api.auth.identity` can resolve the numeric id.
ME = "users/me"

#: Page cap for everything except the two session types. The API returns at most this
#: many points per page *and defaults to a far smaller page* - 1 440 for ``rollUp``,
#: 90 for ``dailyRollUp`` - which is why every request in here sets it explicitly.
PAGE_MAX = 10_000
#: ``sleep`` and ``exercise`` are capped at 25 per page, default and maximum both.
SESSION_PAGE_MAX = 25

#: Rollup range caps, in days. Four data types get 14 days; every other one gets 90.
ROLLUP_MAX_RANGE_DAYS = 90
#: "A maximum range of 14 days for calories-in-heart-rate-zone, heart-rate,
#: active-minutes and total-calories" (ADR-0002 §6, quoting /health/endpoints).
ROLLUP_MAX_RANGE_DAYS_SHORT = 14

#: heart-rate is stored at 1-second resolution; a 60-second rollup is the documented
#: way to ask for per-minute, and 6 days of per-minute is 8 640 points - one page.
#: Chunk size and page size have to be chosen together (ADR-0002 §6), which is what
#: this constant is: the largest window that still fits a single page.
HEART_RATE_WINDOW_DAYS = 6
HEART_RATE_ROLLUP_SECONDS = 60

#: Daily rollups are asked for a month at a time: small enough that one page is
#: guaranteed, big enough that a year is five requests per data type.
DAILY_CHUNK_DAYS = 14  # the shortest of the caps that apply (active-minutes, total-calories)
DAILY_ROLLUP_WINDOW_DAYS = 14
#: A week of sleep sessions: at most one or two sessions a day against a page cap of
#: 25, so a chunk is normally one page and pagination is the exception, not the rule.
SLEEP_CHUNK_DAYS = 7


@dataclass(frozen=True)
class Payload:
    """One response body, plus the first day the request asked for.

    A v4 response carries its own timestamps in every point, so ``day`` is provenance
    rather than a fact the normalizer has to invent: it says which window the request
    covered, which is what a report line and a watermark are keyed on.
    """

    day: date | None
    body: dict[str, Any]


@dataclass(frozen=True)
class Request:
    """One API call.

    A GET ``list`` carries its window in a ``filter`` query string; a ``rollUp`` or
    ``dailyRollUp`` carries it in a JSON body, because those are POSTs. ``covers`` is
    the set of days the *answer* describes (a 14-day rollup answers for 14 days), and
    ``pages`` is the plan's honest estimate of how many HTTP calls this costs, which is
    what ``--dry-run`` adds up and what a chunk's budget is measured against.
    """

    template: str
    day: date
    covers: tuple[date, ...] = ()
    method: str = "GET"
    query: tuple[tuple[str, str], ...] = ()
    body: Mapping[str, Any] | None = None
    #: Extra ``str.format`` keys for the template (``date`` is always available).
    parts: tuple[tuple[str, str], ...] = ()
    #: Key of the point list inside the response: ``dataPoints`` or ``rollupDataPoints``.
    items: str = "dataPoints"
    pages: int = 1

    def path(self) -> str:
        rendered = self.template.format(date=self.day.isoformat(), **dict(self.parts))
        return rendered if rendered.startswith("/") else f"/{rendered}"

    def params(self) -> dict[str, str]:
        return dict(self.query)

    def json_body(self) -> dict[str, Any] | None:
        return dict(self.body) if self.body is not None else None

    def page_token(self, response: Mapping[str, Any]) -> str | None:
        """The token for the next page of this request, if the API offered one."""
        token = response.get("nextPageToken")
        text = str(token or "").strip()
        return text or None

    def next_page(self, token: str) -> Request:
        """The same request moved to ``token`` - in the body for a POST, the query for a GET.

        The two verbs carry the token in different places, and getting that wrong is a
        silent "page 1 again": a POST that ignores its body token re-asks for the first
        page forever.
        """
        if self.body is not None:
            merged = dict(self.body)
            merged["pageToken"] = token
            return replace(self, body=merged)
        return replace(self, query=self.query + (("pageToken", token),))

    def label(self) -> str:
        """Short human description of what this request asks for, for reports."""
        verb = self.body.get("windowSize") if isinstance(self.body, Mapping) else None
        if verb:
            return f"{self.method} {self.path()} ({verb} windows)"
        return f"{self.method} {self.path()}"


@dataclass(frozen=True)
class Dataset:
    """One importable dataset and the recipe for fetching and reading it."""

    name: str
    scopes: tuple[str, ...]
    summary: str
    chunk_days: int = 1
    #: ``(payloads, timezone) -> [(ts, values)]``.
    normalize: Callable[[Sequence[Any], str], list[tuple[datetime, dict[str, float]]]] = field(
        default=lambda payloads, tz: []
    )
    #: ``(chunk_start, chunk_stop) -> [Request]`` - what one chunk of this dataset costs.
    requests_in_chunk: Callable[[date, date], Sequence[Request]] = lambda start, stop: ()

    def request_cost(self, start: date, stop: date) -> int:
        """Planned HTTP calls, counting expected pages rather than just requests."""
        return sum(request.pages for request in self.requests_in_chunk(start, stop))

    @property
    def scope(self) -> str:
        """The scope this dataset cannot work without - for single-scope callers."""
        return self.scopes[0]


def daterange(start: date, stop: date) -> Iterable[date]:
    """Calendar days ``[start, stop]`` inclusive."""
    day = start
    while day <= stop:
        yield day
        day += timedelta(days=1)


def _windows(start: date, stop: date, span_days: int) -> Iterable[tuple[date, date]]:
    """``[start, stop]`` cut into closed-open windows of at most ``span_days`` days.

    The API refuses a rollup range longer than its cap for that data type, and a user
    may set ``[import] chunk_days`` above that cap, so the catalog - not the user -
    absorbs the difference. A 30-day chunk of heart rate becomes three rollups, not a
    ``400 INVALID_RANGE``.
    """
    span = max(1, span_days)
    day = start
    while day <= stop:
        end = min(stop, day + timedelta(days=span - 1))
        yield day, end
        day = end + timedelta(days=1)


def _stamp(day: date) -> str:
    """A physical-time literal for a ``range``: RFC 3339, always UTC, always ``Z``."""
    return datetime.combine(day, time.min, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _civil(day: date) -> dict[str, Any]:
    """A ``google.type.CivilDate`` for a ``dailyRollUp`` range."""
    return {"date": {"year": day.year, "month": day.month, "day": day.day}}


def _estimated_pages(points: int, page_size: int) -> int:
    return max(1, -(-points // max(1, page_size)))


# --- request builders ------------------------------------------------------
def rollup_requests(
    data_type: str,
    *,
    window_size: str,
    window_days: int,
    max_range_days: int = ROLLUP_MAX_RANGE_DAYS,
    page_size: int = PAGE_MAX,
    points_per_day: int = 0,
) -> Callable[[date, date], Sequence[Request]]:
    """``POST users/me/dataTypes/<type>/dataPoints:rollUp`` per physical-time window.

    Rollups are what the API calls a *reconciled* stream: overlapping records from
    several sources are merged before aggregation, which is both cheaper than ``list``
    and closer to what a person sees in the app (ADR-0002 §6).
    """
    span = min(window_days, max_range_days)

    def build(start: date, stop: date) -> Sequence[Request]:
        out: list[Request] = []
        for first, last in _windows(start, stop, span):
            days = (last - first).days + 1
            # The range is closed-open, so the exclusive end is the midnight *after* the
            # last day covered - not 23:59:59, which would quietly drop the last second
            # of every window and shorten the final rollup bucket by a minute.
            body: dict[str, Any] = {
                "range": {
                    "startTime": _stamp(first),
                    "endTime": _stamp(last + timedelta(days=1)),
                },
                "windowSize": window_size,
                "pageSize": page_size,
            }
            out.append(
                Request(
                    template=f"{ME}/dataTypes/{data_type}/dataPoints:rollUp",
                    day=first,
                    covers=tuple(daterange(first, last)),
                    method="POST",
                    body=body,
                    items="rollupDataPoints",
                    pages=_estimated_pages(points_per_day * days, page_size) if points_per_day else 1,
                )
            )
        return tuple(out)

    return build


def daily_rollup_requests(
    data_type: str,
    *,
    window_size_days: int = 1,
    window_days: int = DAILY_ROLLUP_WINDOW_DAYS,
    max_range_days: int = ROLLUP_MAX_RANGE_DAYS,
    page_size: int = PAGE_MAX,
) -> Callable[[date, date], Sequence[Request]]:
    """``POST …/dataPoints:dailyRollUp`` per civil-time window.

    The range here is a *civil* interval - a calendar date in the account's timezone,
    not an instant - which is what makes a daily rollup the right call for a daily row.
    The end is exclusive, so it is the day after the last day covered.
    """
    span = min(window_days, max_range_days)

    def build(start: date, stop: date) -> Sequence[Request]:
        out: list[Request] = []
        for first, last in _windows(start, stop, span):
            body: dict[str, Any] = {
                "range": {"start": _civil(first), "end": _civil(last + timedelta(days=1))},
                "windowSizeDays": window_size_days,
                "pageSize": page_size,
            }
            out.append(
                Request(
                    template=f"{ME}/dataTypes/{data_type}/dataPoints:dailyRollUp",
                    day=first,
                    covers=tuple(daterange(first, last)),
                    method="POST",
                    body=body,
                    items="rollupDataPoints",
                )
            )
        return tuple(out)

    return build


def list_requests(
    data_type: str,
    filter_field: str,
    *,
    page_size: int = PAGE_MAX,
    time_of_day: bool = False,
) -> Callable[[date, date], Sequence[Request]]:
    """``GET …/dataPoints`` with a ``filter`` on one time field.

    For a *daily* data type this is the whole story: the points are already one per
    day, so aggregating them again would only lose information.

    Two wire details are easy to get wrong and are the reason this is a function and
    not an f-string in a dataset definition: the data type is snake_case in the filter
    while kebab-case in the path (hyphens here are an ``INVALID_DATA_POINT_FILTER``),
    and only ``>=`` and ``<=`` exist as comparators, so the upper bound of a civil day
    is ``T23:59:59`` rather than the next morning.
    """
    span = ROLLUP_MAX_RANGE_DAYS  # a list has no range cap of its own; keep chunks uniform

    def build(start: date, stop: date) -> Sequence[Request]:
        out: list[Request] = []
        for first, last in _windows(start, stop, span):
            if time_of_day:
                low, high = f'{first.isoformat()}T00:00:00', f"{last.isoformat()}T23:59:59"
            else:
                low, high = first.isoformat(), last.isoformat()
            expression = f'{filter_field} >= "{low}" AND {filter_field} <= "{high}"'
            out.append(
                Request(
                    template=f"{ME}/dataTypes/{data_type}/dataPoints",
                    day=first,
                    covers=tuple(daterange(first, last)),
                    query=(("filter", expression), ("pageSize", str(min(page_size, PAGE_MAX)))),
                    pages=_estimated_pages((last - first).days + 1, page_size),
                )
            )
        return tuple(out)

    return build


def daily_summary_requests(start: date, stop: date) -> Sequence[Request]:
    """One dataset, five data types, two scopes.

    Steps, active minutes, calories and floors are interval types whose daily view is a
    ``dailyRollUp``; resting heart rate is *already* a daily type and must be read with
    ``list``. That second half is not a stylistic choice: ``dailyRollUp`` on
    ``daily-resting-heart-rate`` answers with ``restingHeartRatePersonalRange`` - the
    bounds of the user's personal range - and never with the day's beats per minute.
    Asking for the day's value through the aggregation endpoint is a silent
    wrong-number bug, so the daily type is read raw.
    """
    requests: list[Request] = []
    for builder in (
        daily_rollup_requests("steps", window_days=DAILY_ROLLUP_WINDOW_DAYS),
        daily_rollup_requests(
            "active-minutes", window_days=DAILY_ROLLUP_WINDOW_DAYS, max_range_days=ROLLUP_MAX_RANGE_DAYS_SHORT
        ),
        daily_rollup_requests(
            "total-calories", window_days=DAILY_ROLLUP_WINDOW_DAYS, max_range_days=ROLLUP_MAX_RANGE_DAYS_SHORT
        ),
        daily_rollup_requests("floors", window_days=DAILY_ROLLUP_WINDOW_DAYS),
    ):
        requests.extend(builder(start, stop))
    requests.extend(
        list_requests("daily-resting-heart-rate", "daily_resting_heart_rate.date")(start, stop)
    )
    return tuple(requests)


# --- normalizers -----------------------------------------------------------
def numbers(*pairs: tuple[Any, str]) -> dict[str, float]:
    """``{field: value}`` from ``(raw, field)`` pairs, dropping what is not a number.

    v4 answers with JSON *strings* for every int64 field (``beatsPerMinute``,
    ``countSum``) and JSON numbers for the doubles, so a value that is an integer in
    the docs can arrive either way. Anything that is not a number is skipped rather
    than raising: one odd field must not cost a whole chunk.
    """
    out: dict[str, float] = {}
    for raw, name in pairs:
        if isinstance(raw, bool) or raw is None:
            continue
        if isinstance(raw, (int, float)):
            out[name] = float(raw)
            continue
        if isinstance(raw, str):
            try:
                out[name] = float(raw)
            except ValueError:
                continue
    return out


def _points(payloads: Sequence[Any], key: str) -> Iterable[dict[str, Any]]:
    """The data points under ``key`` (``dataPoints`` or ``rollupDataPoints``) in each body.

    A body that answers with something other than a list where the list should be is
    raised, not skipped: yielding nothing for a shape this build has never seen is how a
    changed API turns into an import that quietly stores zero rows and exits 0.
    """
    for payload in bodies(payloads):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise ValueError(f"{key} was {type(value).__name__}, not a list")
        for point in value:
            if isinstance(point, dict):
                yield point


def normalize_heart_rate(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """A 60-second ``heart-rate`` rollup -> one row per window.

    ``hr`` is the window's average - the value a chart and a resting-HR baseline both
    want - and the min and max come along for free in the same response, so they are
    kept: a minimum over a minute is a different claim from an average, and throwing it
    away would make the old per-minute intraday impossible to reconstruct.
    """
    rows: list[tuple[datetime, dict[str, float]]] = []
    for point in _points(payloads, "rollupDataPoints"):
        ts = _ts(point.get("startTime"), tz_name)
        if ts is None:
            continue
        heart = point.get("heartRate") or {}
        values = numbers(
            (heart.get("beatsPerMinuteAvg"), "hr"),
            (heart.get("beatsPerMinuteMin"), "hr_min"),
            (heart.get("beatsPerMinuteMax"), "hr_max"),
        )
        if values:
            rows.append((ts, values))
    return rows


def normalize_daily_summary(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """Four daily rollups and one daily list -> one row per civil day.

    Every shape here writes to the midnight of the civil date it describes, so the
    ingest layer's merge (ADR-0003 §3) puts them in one row even though they arrive as
    five responses under two different scopes.
    """
    rows: list[tuple[datetime, dict[str, float]]] = []
    for point in _points(payloads, "rollupDataPoints"):
        ts = _civil_start(point.get("civilStartTime"))
        if ts is None:
            continue
        values: dict[str, float] = {}
        values.update(
            numbers(
                ((point.get("steps") or {}).get("countSum"), "steps"),
                ((point.get("totalCalories") or {}).get("kcalSum"), "calories"),
                ((point.get("floors") or {}).get("countSum"), "floors"),
                ((point.get("distance") or {}).get("millimetersSum"), "distance_m"),
            )
        )
        # active-minutes rolls up *per intensity*, so the day's total is a sum of the
        # levels; a missing level is zero, not a hole.
        levels = (point.get("activeMinutes") or {}).get("activeMinutesRollupByActivityLevel")
        if isinstance(levels, list):
            total = 0.0
            counted = False
            for level in levels:
                minutes = (level or {}).get("minutes") if isinstance(level, dict) else None
                value = numbers((minutes, "m")).get("m")
                if value is not None:
                    total += value
                    counted = True
            if counted:
                values["active_minutes"] = total
        if values:
            rows.append((ts, values))
    for point in _points(payloads, "dataPoints"):
        daily = point.get("dailyRestingHeartRate")
        if not isinstance(daily, dict):
            continue
        ts = _civil_date(daily.get("date"))
        values = numbers((daily.get("beatsPerMinute"), "resting_hr"))
        if ts is not None and values:
            rows.append((ts, values))
    return rows


def normalize_sleep(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """``sleep`` sessions -> one row per session, naps included.

    The durations come from ``summary`` (the API's own totals, including the sum of all
    stages) and the per-stage minutes from ``stagesSummary``, keyed by stage name.
    ``awake_minutes`` is total time awake and ``awakenings`` is how many awake segments
    that was, which is not the same number and never was.

    ``efficiency`` is *derived* here - minutes asleep over minutes in the sleep period -
    because v4 publishes the two parts and no efficiency figure; a device-reported
    efficiency is not available and inventing one would be worse than computing it in the
    open.
    """
    rows: list[tuple[datetime, dict[str, float]]] = []
    for point in _points(payloads, "dataPoints"):
        session = point.get("sleep")
        if not isinstance(session, dict):
            continue
        interval = session.get("interval") or {}
        ts = _ts(interval.get("startTime"), tz_name) or _civil_start(interval.get("civilStartTime"))
        if ts is None:
            continue
        summary = session.get("summary") or {}
        values = numbers(
            (summary.get("minutesAsleep"), "sleep_minutes"),
            (summary.get("minutesAwake"), "awake_minutes"),
            (summary.get("minutesInSleepPeriod"), "time_in_bed_minutes"),
            (summary.get("minutesToFallAsleep"), "minutes_to_fall_asleep"),
            (summary.get("minutesAfterWakeUp"), "minutes_after_wake"),
        )
        stages = summary.get("stagesSummary")
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                name = str(stage.get("type") or "").strip().upper()
                field_name = {"DEEP": "deep_minutes", "LIGHT": "light_minutes", "REM": "rem_minutes"}.get(name)
                if field_name:
                    values.update(numbers((stage.get("minutes"), field_name)))
                elif name == "AWAKE":
                    values.update(numbers((stage.get("count"), "awakenings")))
        asleep = numbers((summary.get("minutesAsleep"), "a")).get("a")
        in_bed = numbers((summary.get("minutesInSleepPeriod"), "b")).get("b")
        if asleep is not None and in_bed:
            values["efficiency"] = round(100.0 * asleep / in_bed, 1)
        if values:
            rows.append((ts, values))
    return rows


def normalize_hrv(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """``daily-heart-rate-variability`` -> one row per civil day.

    ``rmssd`` is the API's ``averageHeartRateVariabilityMilliseconds``, which is the
    same measurement the legacy ``hrvValue`` was. The legacy ``sdnn`` and ``hhv_n``
    fields have no v4 equivalent on this data type, so they simply stop arriving:
    plugins that ask for a field that is not there get a gap, which is the honest
    answer, rather than a zero.
    """
    rows: list[tuple[datetime, dict[str, float]]] = []
    for point in _points(payloads, "dataPoints"):
        daily = point.get("dailyHeartRateVariability")
        if not isinstance(daily, dict):
            continue
        ts = _civil_date(daily.get("date"))
        values = numbers(
            (daily.get("averageHeartRateVariabilityMilliseconds"), "rmssd"),
            (daily.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds"), "deep_sleep_rmssd"),
            (daily.get("entropy"), "entropy"),
            (daily.get("nonRemHeartRateBeatsPerMinute"), "non_rem_hr"),
        )
        if ts is not None and values:
            rows.append((ts, values))
    return rows


def normalize_spo2(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """``daily-oxygen-saturation`` -> one row per civil day.

    v4 publishes an average and a *confidence interval*, not an observed low and high.
    They go into ``spo2_min``/``spo2_max`` because that is the field pair every existing
    consumer reads, but the difference is real and is recorded in
    :ref:`docs/data-import.md`: a lower bound of 88% is not a reading of 88%.
    """
    rows: list[tuple[datetime, dict[str, float]]] = []
    for point in _points(payloads, "dataPoints"):
        daily = point.get("dailyOxygenSaturation")
        if not isinstance(daily, dict):
            continue
        ts = _civil_date(daily.get("date"))
        values = numbers(
            (daily.get("averagePercentage"), "spo2_avg"),
            (daily.get("lowerBoundPercentage"), "spo2_min"),
            (daily.get("upperBoundPercentage"), "spo2_max"),
            (daily.get("standardDeviationPercentage"), "spo2_stdev"),
        )
        if ts is not None and values:
            rows.append((ts, values))
    return rows


def bodies(payloads: Sequence[Any]) -> list[dict[str, Any]]:
    """Accept either :class:`Payload` objects or bare dicts (tests pass dicts)."""
    out: list[dict[str, Any]] = []
    for payload in payloads:
        if isinstance(payload, Payload):
            out.append(dict(payload.body))
        elif isinstance(payload, dict):
            out.append(payload)
    return out


def _ts(text: Any, tz_name: str) -> datetime | None:
    """A physical timestamp. v4 always carries an offset, so none is assumed twice."""
    if not text:
        return None
    try:
        return parse_timestamp(str(text), tz_name=tz_name, field="startTime")
    except UsageError:
        return None


def _civil_date(value: Any) -> datetime | None:
    """A ``google.type.Date`` -> UTC midnight of that civil date."""
    if not isinstance(value, Mapping):
        return None
    try:
        return datetime(int(value["year"]), int(value["month"]), int(value["day"]), tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        return None


def _civil_start(value: Any) -> datetime | None:
    """A ``google.type.CivilDateTime`` -> UTC midnight of its date."""
    if not isinstance(value, Mapping):
        return None
    return _civil_date(value.get("date"))


def _day_start(text: Any) -> datetime:
    """Backwards-compatible day floor for a ``date``/``datetime``/ISO string."""
    if isinstance(text, date) and not isinstance(text, datetime):
        return datetime(text.year, text.month, text.day, tzinfo=UTC)
    if isinstance(text, datetime):
        return datetime(text.year, text.month, text.day, tzinfo=UTC)
    parsed = _civil_date({"year": str(text)[0:4], "month": str(text)[5:7], "day": str(text)[8:10]})
    return parsed or datetime(1970, 1, 1, tzinfo=UTC)


def perform(client: Any, request: Request) -> Any:
    """Issue one :class:`Request` through a client, with the verb the catalog chose.

    Lives here rather than in the importer or the client because the *catalog* is what
    decides that a ``list`` is a filtered GET and a ``rollUp`` is a JSON POST, and both
    the importer and ``fm api probe`` have to honour that decision identically. Two
    copies of this is how one of them ends up asking a rollup endpoint with a GET and
    reading the unaggregated answer as an empty day.
    """
    body = request.json_body()
    if body is not None:
        return client.request(request.method, request.path(), request.params(), body)
    if request.method.upper() == "GET":
        return client.get(request.path(), request.params())
    return client.request(request.method, request.path(), request.params())


# --- the catalog -----------------------------------------------------------
#: Every dataset ``fm import`` knows, keyed by name. The names are ours, not the
#: vendor's: ``series.dataset``, the seed corpus, plugin ``datasets`` tuples and the
#: typed views all speak these five words, and the data-type names stop at this file
#: (ADR-0002 §7).
CATALOG: dict[str, Dataset] = {
    "heart_rate": Dataset(
        name="heart_rate",
        scopes=(HEALTH_METRICS_SCOPE,),
        summary="heart rate, one point per minute (60-second rollups)",
        chunk_days=HEART_RATE_WINDOW_DAYS,
        normalize=normalize_heart_rate,
        requests_in_chunk=rollup_requests(
            "heart-rate",
            window_size=f"{HEART_RATE_ROLLUP_SECONDS}s",
            window_days=HEART_RATE_WINDOW_DAYS,
            max_range_days=ROLLUP_MAX_RANGE_DAYS_SHORT,
            # 1 440 minutes a day against a 10 000-point page: a 6-day window is one
            # page, a 14-day one is three. The plan says so rather than finding out.
            points_per_day=1440,
        ),
    ),
    "daily_summary": Dataset(
        name="daily_summary",
        scopes=(HEALTH_METRICS_SCOPE, ACTIVITY_SCOPE),
        summary="daily steps, calories, active minutes, floors and resting HR",
        chunk_days=DAILY_CHUNK_DAYS,
        normalize=normalize_daily_summary,
        requests_in_chunk=daily_summary_requests,
    ),
    "sleep": Dataset(
        name="sleep",
        scopes=(SLEEP_SCOPE,),
        summary="sleep sessions: asleep/awake/stage minutes, naps included",
        chunk_days=SLEEP_CHUNK_DAYS,
        normalize=normalize_sleep,
        # Keyed on civil *end* time: a night belongs to the day it woke up in, and
        # that is the only way a chunk boundary does not split a session in half. The
        # bounds carry a clock time because a bare civil date means midnight, and
        # "midnight <= session end <= midnight" would match exactly nothing.
        requests_in_chunk=list_requests(
            "sleep", "sleep.interval.civil_end_time", page_size=SESSION_PAGE_MAX, time_of_day=True
        ),
    ),
    "hrv": Dataset(
        name="hrv",
        scopes=(HEALTH_METRICS_SCOPE,),
        summary="daily HRV (RMSSD) as reported by the account",
        chunk_days=DAILY_CHUNK_DAYS,
        normalize=normalize_hrv,
        requests_in_chunk=list_requests("daily-heart-rate-variability", "daily_heart_rate_variability.date"),
    ),
    "spo2": Dataset(
        name="spo2",
        scopes=(HEALTH_METRICS_SCOPE,),
        summary="nightly SpO2 average and confidence interval",
        chunk_days=DAILY_CHUNK_DAYS,
        normalize=normalize_spo2,
        requests_in_chunk=list_requests("daily-oxygen-saturation", "daily_oxygen_saturation.date"),
    ),
}


def lookup(name: str) -> Dataset:
    """The dataset named on the command line, or a usage error naming the valid ones."""
    try:
        return CATALOG[name]
    except KeyError:
        raise UsageError(
            f"unknown dataset {name!r}",
            hint="choose from: " + ", ".join(sorted(CATALOG)),
        ) from None


def resolve_names(requested: Sequence[str] | None, *, allowed: Iterable[str] | None = None) -> tuple[str, ...]:
    """Validate a ``--dataset`` list, or fall back to every dataset the account allows."""
    if requested:
        seen: list[str] = []
        for name in requested:
            for part in str(name).split(","):
                part = part.strip()
                if not part:
                    continue
                lookup(part)
                if part not in seen:
                    seen.append(part)
        if not seen:
            raise UsageError("no datasets selected", hint="e.g. --dataset heart_rate --dataset sleep")
        return tuple(seen)
    if allowed is None:
        return tuple(sorted(CATALOG))
    permitted = [name for name in sorted(CATALOG) if name in set(allowed)]
    if not permitted:
        raise UsageError(
            "this account exposes no dataset fm knows how to import",
            hint="check the granted scopes with `fm auth status`, or pass --dataset explicitly",
        )
    return tuple(permitted)


def short_scope(scope: str) -> str:
    """``…googlehealth.sleep.readonly`` -> ``sleep.readonly``: a scope a human can read."""
    return scope[len(SCOPE_PREFIX) :] if scope.startswith(SCOPE_PREFIX) else scope


def permitted_datasets(profile: Mapping[str, Any], scopes: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(importable names, reasons for the ones skipped)`` from profile + token scopes.

    This is "everything the account allows". Under v4 the *scope* is the whole answer:
    ``users/getProfile`` carries no per-dataset resource list the way the legacy
    ``profile.resources`` did, so there is nothing for a profile to veto and a granted
    scope is enough to ask. (The profile still earns its place: ``membershipStartDate``
    is what tells ``--full-history`` where the history starts.)

    A dataset that needs more than one scope needs all of them, and the skip reason
    names exactly which one is missing, because Google's own rule for partial consent
    is "keep your application functional using the permitted data streams" and never
    "guess". Skipping with a named reason is that rule at dataset granularity.
    """
    granted = {str(s) for s in scopes}
    names: list[str] = []
    skipped: list[str] = []
    for name in sorted(CATALOG):
        dataset = CATALOG[name]
        missing = [scope for scope in dataset.scopes if scope not in granted]
        if granted and missing:
            skipped.append(f"{name}: token has no {short_scope(missing[0])!r} scope")
            continue
        names.append(name)
    return tuple(names), tuple(skipped)


__all__ = [
    "ACTIVITY_SCOPE",
    "ALL_SCOPES",
    "CATALOG",
    "FIELDS",
    "HEALTH_METRICS_SCOPE",
    "PAGE_MAX",
    "PROFILE_SCOPE",
    "SESSION_PAGE_MAX",
    "SLEEP_SCOPE",
    "Dataset",
    "Payload",
    "Request",
    "bodies",
    "daily_rollup_requests",
    "daily_summary_requests",
    "daterange",
    "list_requests",
    "lookup",
    "normalize_daily_summary",
    "normalize_heart_rate",
    "normalize_hrv",
    "normalize_sleep",
    "normalize_spo2",
    "permitted_datasets",
    "perform",
    "resolve_names",
    "rollup_requests",
    "short_scope",
]
