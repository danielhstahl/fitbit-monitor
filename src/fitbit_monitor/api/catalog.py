"""The dataset catalog: what to ask the API for, and how to read the answer.

One :class:`Dataset` per importable dataset. Each one knows four things, and that is
the whole contract:

* which **scope** it needs and which ``profile.resources`` entry implies it - that is
  how ``fm import`` with no ``--dataset`` resolves "everything the account allows";
* how many **days** one request covers (``chunk_days``), which is set by the API's
  own shape: intraday heart rate is one request per day, HRV is one per week;
* the **requests** for a date window, in order;
* how to **normalise** the responses into ``(timestamp, {field: value})`` rows.

Anything that is true for every dataset - chunking a window, counting requests,
storing payloads, upserting rows, advancing watermarks - lives in
:mod:`fitbit_monitor.ingest.importer`, so adding a dataset means adding 30 lines
here and a fixture, not touching the importer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..errors import UsageError
from ..store.seed import DATASETS as SEED_DATASETS
from ..timeutil import parse_timestamp

#: Fields each dataset is expected to carry, for reporting and for ``fm dev seed`` parity.
FIELDS: dict[str, tuple[str, ...]] = dict(SEED_DATASETS)


@dataclass(frozen=True)
class Payload:
    """One response body, plus the day the request asked for.

    Several Fitbit endpoints answer about a day without repeating the date in the body
    (``activities/date/D.json`` is one), so the day has to travel with the payload
    rather than being guessed out of it.
    """

    day: date | None
    body: dict[str, Any]


@dataclass(frozen=True)
class Request:
    """One API call: a path template, the day its path is built from, and the days
    its response answers for (an HRV request for Monday covers the week)."""

    template: str
    day: date
    covers: tuple[date, ...] = ()

    def path(self) -> str:
        return self.template.format(date=self.day.isoformat())


@dataclass(frozen=True)
class Dataset:
    """One importable dataset and the recipe for fetching and reading it."""

    name: str
    scope: str
    summary: str
    chunk_days: int = 1
    #: ``profile.resources`` entry that says the account has this at all (None = assume yes).
    resource: str | None = None
    #: ``(payloads, timezone) -> [(ts, values)]``.
    normalize: Callable[[Sequence[Any], str], list[tuple[datetime, dict[str, float]]]] = field(
        default=lambda payloads, tz: []
    )
    #: ``(chunk_start, chunk_stop) -> [Request]`` - what one chunk of this dataset costs.
    requests_in_chunk: Callable[[date, date], Sequence[Request]] = lambda start, stop: ()

    def request_cost(self, start: date, stop: date) -> int:
        return len(self.requests_in_chunk(start, stop))


def daterange(start: date, stop: date) -> Iterable[date]:
    """Calendar days ``[start, stop]`` inclusive."""
    day = start
    while day <= stop:
        yield day
        day += timedelta(days=1)


def per_day(template: str) -> Callable[[date, date], Sequence[Request]]:
    """One request per day - how Fitbit addresses intraday and per-day datasets."""
    return lambda start, stop: tuple(Request(template, day, (day,)) for day in daterange(start, stop))


def per_chunk(template: str) -> Callable[[date, date], Sequence[Request]]:
    """One request for the whole chunk - HRV's ``7d`` window, once per chunk."""
    return lambda start, stop: (Request(template, start, tuple(daterange(start, stop))),)


def daily_summary_requests(start: date, stop: date) -> Sequence[Request]:
    """Steps and calories come from one endpoint, resting HR from another: two requests
    per day, both feeding the same ``daily_summary`` row."""
    out: list[Request] = []
    for day in daterange(start, stop):
        out.append(Request("/1/user/-/activities/date/{date}.json", day, (day,)))
        out.append(Request("/1/user/-/activities/heart/date/{date}/1d.json", day, (day,)))
    return tuple(out)


def _day(ts: datetime) -> datetime:
    return datetime(ts.year, ts.month, ts.day, tzinfo=UTC)


def _ts(text: Any, tz_name: str) -> datetime | None:
    if not text:
        return None
    try:
        return parse_timestamp(str(text), tz_name=tz_name, field="dateTime")
    except UsageError:
        return None


# --- normalizers -----------------------------------------------------------
def normalize_heart_intraday(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """``/1/user/-/heart/date/D/1d/1min.json`` -> one row per minute."""
    rows: list[tuple[datetime, dict[str, float]]] = []
    for payload in bodies(payloads):
        for entry in payload.get("heart") or []:
            ts = _ts(entry.get("dateTime"), tz_name)
            value = entry.get("heartRate")
            if ts is None or not isinstance(value, (int, float)):
                continue
            rows.append((ts, {"hr": float(value)}))
    return rows


def normalize_daily_summary(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """``activities/date/D.json`` + ``activities/heart/date/D/1d.json`` -> one row/day.

    Both endpoints describe the same day, so both write to the same instant and the
    ingest layer merges the fields into one row.
    """
    rows: list[tuple[datetime, dict[str, float]]] = []
    for payload in bodies(payloads):
        summary = payload.get("summary")
        payload_day = payload.get("__day__")
        if isinstance(summary, dict) and summary:
            values: dict[str, float] = {}
            for key, field_name in (
                ("steps", "steps"),
                ("calories", "calories"),
                ("activelyMinutes", "active_minutes"),
                ("veryActiveMinutes", "very_active_minutes"),
                ("floors", "floors"),
            ):
                if isinstance(summary.get(key), (int, float)):
                    values[field_name] = float(summary[key])
            if values:
                rows.append((_day_start(payload_day), values))
        for entry in payload.get("activities-heart") or []:
            value = entry.get("value") if isinstance(entry, dict) else None
            if not isinstance(value, dict):
                continue
            values = {}
            for key, field_name in (
                ("restingHeartRate", "resting_hr"),
                ("minimumHeartRate", "min_hr"),
                ("maximumHeartRate", "max_hr"),
            ):
                if isinstance(value.get(key), (int, float)):
                    values[field_name] = float(value[key])
            if values:
                rows.append((_day_start(entry.get("dateTime") or payload_day), values))
    return rows


def bodies(payloads: Sequence[Any]) -> list[dict[str, Any]]:
    """Accept either :class:`Payload` objects or bare dicts (tests pass dicts)."""
    out: list[dict[str, Any]] = []
    for payload in payloads:
        if isinstance(payload, Payload):
            merged = dict(payload.body)
            if payload.day is not None:
                merged["__day__"] = payload.day
            out.append(merged)
        elif isinstance(payload, dict):
            out.append(payload)
    return out


def _day_start(text: Any) -> datetime:
    if isinstance(text, date) and not isinstance(text, datetime):
        return datetime(text.year, text.month, text.day, tzinfo=UTC)
    if isinstance(text, datetime):
        return _day(ensure_aware(text))
    raw = str(text or "")[:10]
    year, month, day = (int(raw[0:4]), int(raw[5:7]), int(raw[8:10])) if len(raw) >= 10 else (1970, 1, 1)
    return datetime(year, month, day, tzinfo=UTC)


def ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def normalize_hrv(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """``/1/user/-/hrv/date/D/7d.json`` -> one row per *daily* entry.

    The endpoint mixes daily figures with per-minute and wake-time entries; only
    ``type == "daily"`` (or a bare date as ``dateTime``) is a stored value, and the
    rest are counted as skipped by the caller through the row/point difference.
    """
    rows: list[tuple[datetime, dict[str, float]]] = []
    for payload in bodies(payloads):
        hrv = payload.get("hrv") or {}
        for entry in hrv.get("heartRateVariabilityDd") or []:
            kind = str(entry.get("type") or "")
            stamp = str(entry.get("dateTime") or "")
            if kind and kind != "daily":
                continue
            ts = _day(parse_timestamp(stamp[:10], tz_name=tz_name, field="hrv dateTime"))
            values: dict[str, float] = {}
            if isinstance(entry.get("hrvValue"), (int, float)):
                values["rmssd"] = float(entry["hrvValue"])
            if isinstance(entry.get("sdnn"), (int, float)):
                values["sdnn"] = float(entry["sdnn"])
            if isinstance(entry.get("hrv_n"), (int, float)):
                values["hrv_n"] = float(entry["hrv_n"])
            if values:
                rows.append((ts, values))
    return rows


def normalize_sleep(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """``/1/user/-/sleep/date/D.json`` -> one row per sleep log (naps included)."""
    rows: list[tuple[datetime, dict[str, float]]] = []
    for payload in bodies(payloads):
        for log in payload.get("sleep") or []:
            if not isinstance(log, dict):
                continue
            ts = _ts(log.get("startTime"), tz_name) or _ts(log.get("dateOfSleep"), tz_name)
            if ts is None:
                continue
            values: dict[str, float] = {}
            for key, field_name in (
                ("minutesAsleep", "sleep_minutes"),
                ("minutesAwake", "awake_minutes"),
                ("timeInBed", "time_in_bed_minutes"),
                ("minutesDeep", "deep_minutes"),
                ("minutesLight", "light_minutes"),
                ("minutesREM", "rem_minutes"),
                ("awakeningsCount", "awakenings"),
                ("efficiency", "efficiency"),
            ):
                if isinstance(log.get(key), (int, float)):
                    values[field_name] = float(log[key])
            if values:
                rows.append((ts, values))
    return rows


def normalize_spo2(payloads: Sequence[Any], tz_name: str) -> list[tuple[datetime, dict[str, float]]]:
    """``/1/user/-/spo2/date/D/1d.json`` -> one row per nightly reading."""
    rows: list[tuple[datetime, dict[str, float]]] = []
    for payload in bodies(payloads):
        readings = payload.get("spo2") or payload.get("spo2Summaries") or []
        for reading in readings:
            if not isinstance(reading, dict):
                continue
            ts = _ts(reading.get("startTime"), tz_name)
            if ts is None:
                continue
            values: dict[str, float] = {}
            for key, field_name in (
                ("averageSPO2", "spo2_avg"),
                ("minSPO2", "spo2_min"),
                ("maxSPO2", "spo2_max"),
            ):
                if isinstance(reading.get(key), (int, float)):
                    values[field_name] = float(reading[key])
            if values:
                rows.append((ts, values))
    return rows


# --- request builders ------------------------------------------------------


#: Every dataset ``fm import`` knows, keyed by name.
CATALOG: dict[str, Dataset] = {
    "heart_rate": Dataset(
        name="heart_rate",
        scope="heartbeat",
        summary="intraday heart rate, one point per minute",
        chunk_days=1,
        resource="heartRate",
        normalize=normalize_heart_intraday,
        requests_in_chunk=per_day("/1/user/-/heart/date/{date}/1d/1min.json"),
    ),
    "daily_summary": Dataset(
        name="daily_summary",
        scope="activity",
        summary="daily steps, calories, active minutes and resting HR",
        chunk_days=1,
        resource="activities",
        normalize=normalize_daily_summary,
        requests_in_chunk=daily_summary_requests,
    ),
    "sleep": Dataset(
        name="sleep",
        scope="sleep",
        summary="sleep logs: asleep/awake/deep minutes per session",
        chunk_days=1,
        resource="sleep",
        normalize=normalize_sleep,
        requests_in_chunk=per_day("/1/user/-/sleep/date/{date}.json"),
    ),
    "hrv": Dataset(
        name="hrv",
        scope="heart_profile",
        summary="daily HRV (RMSSD) as reported by Fitbit",
        chunk_days=7,
        resource=None,
        normalize=normalize_hrv,
        requests_in_chunk=per_chunk("/1/user/-/hrv/date/{date}/7d.json"),
    ),
    "spo2": Dataset(
        name="spo2",
        scope="spo2",
        summary="nightly SpO2 minimum and average",
        chunk_days=1,
        resource=None,
        normalize=normalize_spo2,
        requests_in_chunk=per_day("/1/user/-/spo2/date/{date}/1d.json"),
    ),
}

#: Every scope ``fm auth login`` asks for when config.toml does not name any.
ALL_SCOPES: tuple[str, ...] = tuple(sorted({d.scope for d in CATALOG.values()} | {"profile"}))

#: Scopes that are worth asking for only if the app has them; kept for docs/tests.
OPTIONAL_SCOPES: tuple[str, ...] = tuple(sorted({d.scope for d in CATALOG.values()} - {"heartbeat"}))


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


def permitted_datasets(profile: Mapping[str, Any], scopes: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(importable names, reasons for the ones skipped)`` from profile + token scopes.

    This is "everything the account allows": the scope has to be on the token, and
    where Fitbit's profile says what the account has (``resources``), that gets a
    veto too. Skips are reported with a reason rather than silently dropped.
    """
    granted = {str(s) for s in scopes}
    resources = {str(r) for r in (profile.get("resources") or [])}
    names: list[str] = []
    skipped: list[str] = []
    for name in sorted(CATALOG):
        dataset = CATALOG[name]
        if granted and dataset.scope not in granted:
            skipped.append(f"{name}: token has no {dataset.scope!r} scope")
            continue
        if resources and dataset.resource is not None and dataset.resource not in resources:
            skipped.append(f"{name}: account has no {dataset.resource!r} resource")
            continue
        names.append(name)
    return tuple(names), tuple(skipped)


__all__ = [
    "ALL_SCOPES",
    "CATALOG",
    "FIELDS",
    "OPTIONAL_SCOPES",
    "Dataset",
    "Payload",
    "Request",
    "bodies",
    "daterange",
    "lookup",
    "permitted_datasets",
    "resolve_names",
]
