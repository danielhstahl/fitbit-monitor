#!/usr/bin/env python3
"""Generate the recorded API fixtures, offline, deterministically.

CI has no network and no Google credentials, so the import tests replay recorded
responses from ``tests/fixtures/api``. Those recordings are *made* here: a
deterministic in-process stand-in for the Google Health API (``SyntheticApi``) answers
exactly the requests ``fm import`` makes, and
:class:`~fitbit_monitor.api.http.RecordingTransport` writes what it saw to fixture files.

    python scripts/dev/record_fixtures.py                       # rewrite tests/fixtures/api
    python scripts/dev/record_fixtures.py --days 7 --out /tmp/fx
    python scripts/dev/record_fixtures.py --check               # fail if the fixtures are stale

Shapes follow the live endpoints - they are written against the published ``v4``
discovery schema (field names, the ``rollupDataPoints``/``dataPoints`` envelopes, the
``google.type.Date`` and civil-time objects, int64 fields as JSON *strings*), with three
deliberate differences that keep the fixture set reviewable:

* the ``heart-rate`` rollup answers at a 5-minute cadence, not the 1-second resolution
  the data type is stored at or the 60-second buckets a real window returns;* nothing answers ``404``: the v4 API answers a period with no data with ``200`` and an
  empty list, so two days are simply **absent** from the answers (one heart-rate day,
  one SpO2 day) because "the window came back shorter than it was asked" is a path the
  importer and the watermark logic have to handle;
* the rate-limit headers the legacy API sent are **not** simulated, because ``v4`` sends
  none and a client that only paces itself off headers would be tested against a
  behaviour it will never meet. See :class:`fitbit_monitor.api.client.TokenBucket`.

Every value is generated from a per-day seeded RNG, so the corpus is byte-stable and
``--check`` can tell a stale fixture from a fresh one.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fitbit_monitor.api.auth import CredentialStore, TokenSet  # noqa: E402
from fitbit_monitor.api.catalog import ALL_SCOPES, CATALOG  # noqa: E402
from fitbit_monitor.api.client import HealthClient  # noqa: E402
from fitbit_monitor.api.http import RecordingTransport  # noqa: E402
from fitbit_monitor.config import ApiSettings, ImportSettings  # noqa: E402
from fitbit_monitor.ingest.importer import ImportPlanner  # noqa: E402
from fitbit_monitor.store.watermarks import Watermarks  # noqa: E402
from fitbit_monitor.store.writer import SeriesStore  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "api"
END_DAY = date(2026, 5, 14)
DAYS = 14
SEED = 20260514
MEMBER_SINCE = date(2022, 11, 8)
HEALTH_USER_ID = "a1b2c3d4-e5f6-7890-1234-567890abcdef"
LEGACY_USER_ID = "4412789"
#: Days with no data at all, so a window that answers shorter than asked is exercised.
MISSING_HR_DAY = date(2026, 5, 5)
MISSING_SPO2_DAY = date(2026, 5, 9)
#: Minutes per generated heart-rate bucket. A real 60-second rollup of a six-day window
#: is 8 640 points; a fixture that big in every file is not reviewable, and a bucket is a
#: bucket - the importer does not know or care what cadence produced it.
HR_STEP_MINUTES = 5

SCOPES = tuple(ALL_SCOPES)


class SyntheticApi:
    """An in-process stand-in for ``health.googleapis.com``, keyed by request path and body.

    It routes on the same four things the real service does: the resource root
    (``users/me``), the data type, the verb (``dataPoints``, ``dataPoints:rollUp``,
    ``dataPoints:dailyRollUp``) and - for the verbs that have one - the window in the
    JSON body or the ``filter`` string.
    """

    def __init__(self, *, start: date, stop: date, seed: int = SEED, tz_name: str = "UTC") -> None:
        self.start = start
        self.stop = stop
        self.tz_name = tz_name
        self.rng = random.Random(seed)
        self.calls = 0

    # --- transport seam -----------------------------------------------------
    def request(self, method: str, url: str, headers=None, body=None):
        from urllib.parse import parse_qsl, urlsplit

        from fitbit_monitor.api.http import Response

        parsed = urlsplit(url)
        path = parsed.path
        for prefix in ("https://health.googleapis.com", "http://health.googleapis.com"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
        if path.startswith("/v4"):
            path = path[len("/v4") :] or "/"
        query = dict(parse_qsl(parsed.query))
        payload_body: dict[str, Any] = {}
        if body:
            raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
            try:
                payload_body = json.loads(raw)
            except ValueError:
                payload_body = {}
        self.calls += 1
        status, payload = self.respond(method.upper(), path, query, payload_body)
        headers_out = {"Content-Type": "application/json"}
        etag = self._etag(path, query, payload_body)
        if etag:
            headers_out["ETag"] = etag
        return Response(status, json.dumps(payload).encode("utf-8"), headers_out)

    @staticmethod
    def _etag(path: str, query: dict[str, str], body: dict[str, Any]) -> str | None:
        """A stable, content-like ETag on the GET list endpoints, like the real API.

        The rollup verbs are POSTs and a POST is not conditional here, which matches the
        client: an ETag on an aggregation would assert that a window's contents never
        change, and a late sync proves that they do.
        """
        if body or ":rollUp" in path or ":dailyRollUp" in path:
            return None
        import hashlib

        seed = f"{path}?{sorted(query.items())}"
        return '"%s"' % hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]

    # --- routing ------------------------------------------------------------
    def respond(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        segments = [s for s in path.split("/") if s]
        if segments[:2] == ["users", "me"] and len(segments) == 3 and segments[2] in ("profile", "identity"):
            payload = self.identity() if segments[2] == "identity" else self.profile()
            return 200, payload
        if len(segments) < 5 or segments[:2] != ["users", "me"] or segments[2] != "dataTypes":
            return self._not_found(path)
        data_type = segments[3]
        verb = segments[4] if len(segments) > 4 else ""
        window = self._window(body, query)
        if verb == "dataPoints:rollUp":
            return 200, self.roll_up(data_type, body, window)
        if verb == "dataPoints:dailyRollUp":
            return 200, self.daily_roll_up(data_type, body, window)
        if verb == "dataPoints" and method == "GET":
            return 200, self.list_points(data_type, query, window)
        return self._not_found(path)

    @staticmethod
    def _not_found(path: str) -> tuple[int, Any]:
        """An unknown path, in the AIP envelope the real API uses."""
        return (
            404,
            {
                "error": {
                    "code": 404,
                    "message": f"Requested entity was not found: {path}",
                    "status": "NOT_FOUND",
                }
            },
        )

    def _window(self, body: dict[str, Any], query: dict[str, str]) -> tuple[date, date]:
        """The closed-open window a request asked for, as inclusive first/last days.

        A rollup states it as physical timestamps, a ``dailyRollUp`` as civil dates, and
        a ``list`` as a filter string - three spellings of the same pair of dates, and the
        only thing the generator needs to answer either one.
        """
        span = body.get("range") if isinstance(body.get("range"), dict) else {}
        if span.get("startTime"):
            first = date.fromisoformat(str(span["startTime"])[:10])
            end = date.fromisoformat(str(span.get("endTime") or span["startTime"])[:10])
            # The end is exclusive, so the day it names is not covered unless it carries
            # a clock time past midnight.
            if str(span.get("endTime") or "").endswith("T00:00:00Z"):
                end -= timedelta(days=1)
            return first, max(first, end)
        if span.get("start"):
            first = _from_civil(span["start"])
            end = _from_civil(span.get("end") or span["start"])
            return first, max(first, end - timedelta(days=1))
        dates = re.findall(r'"(\d{4}-\d{2}-\d{2})[^"]*"', str(query.get("filter", "")))
        if dates:
            return date.fromisoformat(dates[0]), date.fromisoformat(max(dates))
        return self.start, self.stop

    def _pages(self, items: list[Any], body: dict[str, Any], query: dict[str, str], *, envelope: str) -> dict[str, Any]:
        """Slice a page out of ``items`` and add a ``nextPageToken`` when there is more.

        The envelope is named by the caller rather than guessed from the contents, because
        an *empty* page has nothing to guess from, and answering a ``list`` with a
        ``rollupDataPoints`` key - or the other way round - is exactly the sort of shape
        mistake that makes a dataset silently import nothing.

        The point of paging here at all is that the importer's page loop is then exercised
        by a real token on a real second page, not only by a hand-written fixture.
        """
        size = int(body.get("pageSize") or query.get("pageSize") or 10_000)
        size = max(1, size)
        token = str(body.get("pageToken") or query.get("pageToken") or "")
        offset = int(token) if token.isdigit() else 0
        page = items[offset : offset + size]
        out: dict[str, Any] = {envelope: page}
        nxt = offset + size
        if nxt < len(items):
            out["nextPageToken"] = str(nxt)
        return out

    # --- bodies -------------------------------------------------------------
    def profile(self) -> dict[str, Any]:
        """``GET users/me/profile`` - the account. No body measurements, no goals."""
        return {
            "name": "users/me/profile",
            "displayName": "Alex Example",
            "timeZone": "Europe/Berlin",
            "age": 34,
            "membershipStartDate": {"year": MEMBER_SINCE.year, "month": MEMBER_SINCE.month, "day": MEMBER_SINCE.day},
        }

    def identity(self) -> dict[str, Any]:
        """``GET users/me/identity`` - the ids this token is bound to."""
        return {
            "name": "users/me/identity",
            "healthUserId": HEALTH_USER_ID,
            "legacyUserId": LEGACY_USER_ID,
        }

    def roll_up(self, data_type: str, body: dict[str, Any], window: tuple[date, date]) -> dict[str, Any]:
        if data_type != "heart-rate":
            return {"rollupDataPoints": []}
        first, last = window
        buckets: list[dict[str, Any]] = []
        day = first
        while day <= last:
            if day != MISSING_HR_DAY:
                buckets.extend(self.heart_buckets(day))
            day += timedelta(days=1)
        return self._pages(buckets, body, {}, envelope="rollupDataPoints")

    def daily_roll_up(self, data_type: str, body: dict[str, Any], window: tuple[date, date]) -> dict[str, Any]:
        first, last = window
        points: list[dict[str, Any]] = []
        day = first
        while day <= last:
            values = self.daily_values(day).get(data_type)
            if values:
                points.append({"civilStartTime": _civil(day), "civilEndTime": _civil(day + timedelta(days=1)), **values})
            day += timedelta(days=1)
        return self._pages(points, body, {}, envelope="rollupDataPoints")

    def list_points(self, data_type: str, query: dict[str, str], window: tuple[date, date]) -> dict[str, Any]:
        first, last = window
        points: list[dict[str, Any]] = []
        day = first
        while day <= last:
            if data_type == "sleep":
                # A civil day can hold a night and a nap, and the nap belongs to the day
                # it ended on - the same rule the request's filter uses.
                points.extend(self.naps(day))
            point = self.list_value(data_type, day)
            if point:
                points.append(point)
            day += timedelta(days=1)
        return self._pages(points, {}, query, envelope="dataPoints")

    # --- the underlying day, generated once per seed -------------------------
    def list_value(self, data_type: str, day: date) -> dict[str, Any] | None:
        rng = random.Random(f"{SEED}:{data_type}:{day.isoformat()}")
        if data_type == "daily-resting-heart-rate":
            resting = round(54.0 + rng.uniform(-2.5, 3.5), 1)
            return {"dailyRestingHeartRate": {"date": _civil_date(day), "beatsPerMinute": str(int(resting))}}
        if data_type == "daily-heart-rate-variability":
            value = round(38.0 + rng.uniform(-9.0, 26.0), 2)
            return {
                "dailyHeartRateVariability": {
                    "date": _civil_date(day),
                    "averageHeartRateVariabilityMilliseconds": value,
                    "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": round(value * 0.9, 2),
                    "entropy": round(rng.uniform(2.4, 3.6), 3),
                }
            }
        if data_type == "daily-oxygen-saturation":
            if day == MISSING_SPO2_DAY:
                return None
            low = round(rng.uniform(88.5, 94.5), 1)
            average = round(low + rng.uniform(0.8, 3.2), 1)
            return {
                "dailyOxygenSaturation": {
                    "date": _civil_date(day),
                    "averagePercentage": average,
                    "lowerBoundPercentage": low,
                    "upperBoundPercentage": round(min(99.0, average + rng.uniform(0.5, 2.0)), 1),
                }
            }
        if data_type == "sleep":
            return {"sleep": self.sleep_session(day)}
        return None

    def daily_values(self, day: date) -> dict[str, dict[str, Any]]:
        """The daily rollup answer for one day, keyed by data type."""
        rng = random.Random(f"{SEED}:act:{day.isoformat()}")
        weekend = day.weekday() >= 5
        steps = rng.randrange(3200, 15200) + (2000 if weekend else 0)
        calories = float(rng.randrange(1850, 3100))
        active = rng.randrange(12, 96)
        floors = rng.randrange(2, 28)
        distance_m = int(rng.uniform(3.0, 11.0) * 1000)
        quiet = max(0, active - rng.randrange(6, 30))
        return {
            "steps": {"steps": {"countSum": str(steps)}},
            "total-calories": {"totalCalories": {"kcalSum": calories}},
            # Active minutes roll up *by intensity*, so the answer is a list and the
            # day's total is the sum of its levels - the shape is the point of the fixture.
            "active-minutes": {
                "activeMinutes": {
                    "activeMinutesRollupByActivityLevel": [
                        {"activityLevel": "SlightlyActive", "minutes": str(max(0, active - 12))},
                        {"activityLevel": "SomewhatActive", "minutes": str(max(0, active // 3))},
                        {"activityLevel": "VeryActive", "minutes": str(max(0, quiet // 4))},
                    ]
                }
            },
            "floors": {"floors": {"countSum": str(floors)}},
            "distance": {"distance": {"millimetersSum": str(distance_m * 1000)}},
        }

    def heart_buckets(self, day: date) -> list[dict[str, Any]]:
        """A day of heart-rate rollup buckets at :data:`HR_STEP_MINUTES`."""
        rng = random.Random(f"{SEED}:hr:{day.isoformat()}")
        resting = 55.0 + rng.uniform(-2.0, 2.0)
        buckets: list[dict[str, Any]] = []
        minutes = 0
        while minutes < 24 * 60:
            hour = minutes / 60
            base = resting + (0 if hour < 6 else 6 + 9 * min(1.0, max(0.0, (hour - 8) / 6)))
            if 17 <= hour < 18:
                base = 118 + rng.uniform(-8, 14)
            avg = round(min(190.0, max(38.0, base + rng.gauss(0, 2.4))), 1)
            spread = round(abs(rng.gauss(2.0, 1.2)), 1)
            start = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(minutes=minutes)
            buckets.append(
                {
                    "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endTime": (start + timedelta(minutes=HR_STEP_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "heartRate": {
                        "beatsPerMinuteAvg": avg,
                        "beatsPerMinuteMin": round(max(35.0, avg - spread), 1),
                        "beatsPerMinuteMax": round(min(200.0, avg + spread), 1),
                    },
                }
            )
            minutes += HR_STEP_MINUTES
        return buckets

    def sleep_session(self, day: date) -> dict[str, Any]:
        """One night as a ``sleep`` session, plus a nap on some days."""
        rng = random.Random(f"{SEED}:sleep:{day.isoformat()}")
        start = datetime(day.year, day.month, day.day, 23, rng.randrange(0, 50), tzinfo=UTC)
        asleep = rng.randrange(5 * 60 + 10, 8 * 60)
        awake = rng.randrange(4, 60)
        deep = int(asleep * rng.uniform(0.14, 0.24))
        rem = int(asleep * rng.uniform(0.18, 0.27))
        light = asleep - deep - rem
        session = {
            "interval": {
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": (start + timedelta(minutes=asleep + awake)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "startUtcOffset": "+00:00",
                "endUtcOffset": "+00:00",
                "civilStartTime": _civil(day),
                "civilEndTime": _civil(day + timedelta(days=1)),
            },
            "type": "stages",
            "metadata": {"processed": True, "mainSleep": True, "manuallyEdited": False, "nap": False},
            "summary": {
                "minutesAsleep": str(asleep),
                "minutesAwake": str(awake),
                "minutesInSleepPeriod": str(asleep + awake),
                "minutesToFallAsleep": str(rng.randrange(1, 25)),
                "minutesAfterWakeUp": str(rng.randrange(0, 25)),
                "stagesSummary": [
                    {"type": "AWAKE", "count": str(rng.randrange(1, 12)), "minutes": str(awake)},
                    {"type": "LIGHT", "count": str(rng.randrange(3, 9)), "minutes": str(light)},
                    {"type": "DEEP", "count": str(rng.randrange(1, 5)), "minutes": str(deep)},
                    {"type": "REM", "count": str(rng.randrange(2, 6)), "minutes": str(rem)},
                ],
            },
        }
        return session

    def naps(self, day: date) -> list[dict[str, Any]]:
        """An afternoon nap on every seventh day, so one civil day holds two sessions.

        This is a property of the corpus rather than of the API, and it is here because
        "a chunk answered two sessions for one day" is a path the row merge has to prove.
        Returned already wrapped in the ``{"sleep": ...}`` point envelope, like every
        other answer from this endpoint.
        """
        if day.day % 7 != 3:
            return []
        start = datetime(day.year, day.month, day.day, 14, 10, tzinfo=UTC)
        return [
            {
                "sleep": {
                    "interval": {
                        "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "endTime": (start + timedelta(minutes=38)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "startUtcOffset": "+00:00",
                        "endUtcOffset": "+00:00",
                        "civilStartTime": _civil(day),
                        "civilEndTime": _civil(day),
                    },
                    "type": "classic",
                    "metadata": {"processed": True, "mainSleep": False, "nap": True},
                    "summary": {
                        "minutesAsleep": "34",
                        "minutesAwake": "4",
                        "minutesInSleepPeriod": "38",
                        "stagesSummary": [
                            {"type": "AWAKE", "count": "1", "minutes": "4"},
                            {"type": "SLEEP", "count": "1", "minutes": "34"},
                        ],
                    },
                }
            }
        ]


def _civil_date(day: date) -> dict[str, int]:
    return {"year": day.year, "month": day.month, "day": day.day}


def _civil(day: date) -> dict[str, Any]:
    return {"date": _civil_date(day), "time": {"hours": 0, "minutes": 0, "seconds": 0}}


def _from_civil(value: Any) -> date:
    """The day a ``google.type.CivilDate`` or ``CivilDateTime`` names."""
    if isinstance(value, dict):
        inner = value.get("date") if isinstance(value.get("date"), dict) else value
        return date(int(inner["year"]), int(inner["month"]), int(inner["day"]))
    return date.fromisoformat(str(value)[:10])


def build(
    out: Path,
    *,
    days: int = DAYS,
    end: date = END_DAY,
    seed: int = SEED,
    datasets: tuple[str, ...] | None = None,
    probe: bool = True,
) -> list[str]:
    """Record every request a ``days``-day import of ``end`` makes. Returns the keys."""
    synthetic = SyntheticApi(start=end - timedelta(days=days - 1), stop=end, seed=seed)
    recording = RecordingTransport(synthetic, out, indent=None)
    client = HealthClient(
        ApiSettings(),
        CredentialStore(Path(tempfile.mkdtemp()) / "credentials.json"),
        import_settings=ImportSettings(),
        transport=recording,
    )
    with tempfile.TemporaryDirectory() as tmp:
        store = SeriesStore(Path(tmp) / "store.sqlite3")
        planner = ImportPlanner(client, Watermarks(store), ImportSettings())
        # The planner reads scopes off the token to decide what the account allows.
        client.set_tokens(
            TokenSet(
                access_token="recorded",
                refresh_token="recorded",
                expires_at=datetime.now(UTC) + timedelta(days=1),
                scopes=SCOPES,
            )
        )
        plan = planner.build(
            datasets=datasets, since=(end - timedelta(days=days - 1)).isoformat(), until=end.isoformat()
        )
        for entry in plan.active:
            for chunk in entry.chunks:
                for request in chunk.requests:
                    _record_page(client, recording, request)
        if probe:
            _record_probe(client, recording, start=end - timedelta(days=days - 1), end=end)
        store.close()
    _provenance(out, start=end - timedelta(days=days - 1), end=end, seed=seed, keys=recording.recorded, datasets=datasets)
    return recording.recorded


PROVENANCE_NAME = "_provenance.json"


def _provenance(
    out: Path, *, start: date, end: date, seed: int, keys: list[str], datasets: tuple[str, ...] | None
) -> None:
    """Where the corpus came from, written next to it.

    A fixture set that arrived from nowhere gets treated like scripture. This file is the
    answer to "what produced these, over what window, and are they real?" - and the honest
    part of the answer is ``"recorded_from": null``: they are generated by the stand-in in
    this script, because ADR-0002 §10 step 1 (one afternoon with a real Personal-mode
    project) has not happened yet.
    """
    (out / PROVENANCE_NAME).write_text(
        json.dumps(
            {
                "generated_by": "scripts/dev/record_fixtures.py",
                "recorded_from": None,
                "synthetic": True,
                "note": (
                    "Generated by SyntheticApi in the script above, from documented response "
                    "shapes - not captured from health.googleapis.com. It is a contract test of "
                    "what this build expects, not evidence of what the service answers."
                ),
                "start": start.isoformat(),
                "stop": end.isoformat(),
                "seed": seed,
                "datasets": sorted(datasets) if datasets else sorted(CATALOG),
                "fixtures": len(keys),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _record_probe(client: HealthClient, recording: RecordingTransport, *, start: date, end: date) -> None:
    """Record what ``fm api probe`` asks, so the diagnostics are testable offline too.

    A probe samples single days (that is how it bounds "how far back does the data go" in
    ``log2`` requests), and those windows are not the windows an import asks for. A corpus
    that only held the import's windows would make ``fm doctor --probe`` and ``fm api
    probe`` miss every fixture they need - and the one command a user reaches for when
    their setup is quietly broken is the last thing that should be untestable in CI.
    """
    from fitbit_monitor.api.probe import run_probe

    try:
        run_probe(client, since=start, until=end, max_requests=None)
    except Exception:  # noqa: BLE001 - a probe never raises; this is belt and braces
        pass


def _record_page(client: HealthClient, recording: RecordingTransport, request: Any) -> None:
    """Fetch one request and every page it offers, recording all of them.

    The page walk is repeated here rather than reused from the importer because this
    script's job is to *record* what an import will ask for, and asking for page two is
    part of that. Following a token the stand-in hands back is what puts the second page
    in the corpus at all.
    """
    from fitbit_monitor.api.catalog import perform

    cursor = request
    seen: set[str] = set()
    while cursor is not None:
        try:
            response = perform(client, cursor)
        except Exception:  # noqa: BLE001 - a refused request is still a recorded one
            return
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            return
        token = cursor.page_token(body) if isinstance(body, dict) else None
        if not token or token in seen:
            return
        seen.add(token)
        cursor = cursor.next_page(token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="fixture directory (default tests/fixtures/api)")
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--end", default=END_DAY.isoformat())
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--dataset", action="append", help="only these datasets (repeatable)")
    parser.add_argument("--clean", action="store_true", help="delete existing fixtures first")
    parser.add_argument("--check", action="store_true", help="record to a temp dir and fail if files differ")
    args = parser.parse_args(argv)

    end = date.fromisoformat(args.end)
    out = Path(args.out)
    if args.check:
        import filecmp
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "api"
            shutil.copytree(out, probe) if out.exists() else probe.mkdir()
            before = {p.name: p for p in probe.glob("*.json")}
            build(probe, days=args.days, end=end, seed=args.seed)
            recorded = {p.name: p for p in probe.glob("*.json")}
            missing = sorted(set(before) - set(recorded))
            changed = [
                name
                for name, path in sorted(recorded.items())
                if not filecmp.cmp(path, before[name], shallow=False)
                if name in before
            ]
        if missing or changed:
            print(f"{out}: fixtures are stale", file=sys.stderr)
            for name in (missing or changed)[:10]:
                print(f"  differs: {name}", file=sys.stderr)
            print("  fix: python scripts/dev/record_fixtures.py", file=sys.stderr)
            return 1
        print(f"{out}: {len(recorded)} fixtures up to date")
        return 0
    if args.clean and out.exists():
        for path in out.glob("*.json"):
            path.unlink()
    out.mkdir(parents=True, exist_ok=True)
    keys = build(out, days=args.days, end=end, seed=args.seed, datasets=tuple(args.dataset or ()) or None)
    print(f"recorded {len(keys)} fixtures into {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
