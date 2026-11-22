#!/usr/bin/env python3
"""Generate the recorded API fixtures, offline, deterministically.

CI has no network and no Fitbit credentials, so the import tests replay recorded
responses from ``tests/fixtures/api``. Those recordings are *made* here: a
deterministic in-process stand-in for the API (``SyntheticApi``) answers exactly the
requests ``fm import`` makes, and :class:`~fitbit_monitor.api.http.RecordingTransport`
writes what it saw to fixture files.

    python scripts/dev/record_fixtures.py                       # rewrite tests/fixtures/api
    python scripts/dev/record_fixtures.py --days 7 --out /tmp/fx
    python scripts/dev/record_fixtures.py --check               # fail if the fixtures are stale

Shapes follow the live endpoints (field names, nesting, the ``dateTime`` convention of
having **no UTC offset**, the rate-limit headers), with two deliberate differences that
keep the fixture set reviewable:

* intraday heart rate is recorded at a 5-minute cadence (288 points/day) instead of
  the 1-minute 1 440 a real day returns;
* two days are recorded as ``404`` (one heart-rate day, one SpO2 day) because "there
  was nothing for that day" is a path the importer must handle.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fitbit_monitor.api.auth import CredentialStore, TokenSet  # noqa: E402
from fitbit_monitor.api.client import FitbitClient  # noqa: E402
from fitbit_monitor.api.http import RecordingTransport  # noqa: E402
from fitbit_monitor.config import ApiSettings, ImportSettings  # noqa: E402
from fitbit_monitor.ingest.importer import ImportPlanner  # noqa: E402
from fitbit_monitor.store.watermarks import Watermarks  # noqa: E402
from fitbit_monitor.store.writer import SeriesStore  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "api"
END_DAY = date(2026, 5, 14)
DAYS = 14
SEED = 20260514
MEMBER_SINCE = "2022-11-08"
#: One heart-rate day and one SpO2 day that answer 404, so "no data" is exercised.
MISSING_HR_DAY = date(2026, 5, 5)
MISSING_SPO2_DAY = date(2026, 5, 9)
#: Days per intraday HR point (a fixture-size compromise, see the module docstring).
HR_STEP_MINUTES = 5

SCOPES = ("profile", "activity", "sleep", "heartbeat", "heart_profile", "spo2")
RESOURCES = ["profile", "settings", "activities", "body", "weight", "heartRate"]


class SyntheticApi:
    """An in-process stand-in for ``api.fitbit.com``, keyed by request path."""

    def __init__(self, *, start: date, stop: date, seed: int = SEED, tz_name: str = "UTC") -> None:
        self.start = start
        self.stop = stop
        self.tz_name = tz_name
        self.rng = random.Random(seed)
        self.calls = 0
        self.rate_limit_remaining = 150

    # --- transport seam -----------------------------------------------------
    def request(self, method: str, url: str, headers=None, body=None):
        from fitbit_monitor.api.http import Response

        path = url.split("?")[0]
        for prefix in ("https://api.fitbit.com", "http://api.fitbit.com"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
        self.calls += 1
        self.rate_limit_remaining = max(0, self.rate_limit_remaining - 1)
        status, payload = self.respond(path)
        headers_out = {
            "Content-Type": "application/json",
            "X-RateLimit-Limit": "150",
            "X-RateLimit-Limit-Interval": "15m",
            "X-RateLimit-Remaining": str(self.rate_limit_remaining),
            "X-RateLimit-Reset": "847",
        }
        etag = self._etag(path)
        if etag:
            headers_out["ETag"] = etag
        return Response(status, json.dumps(payload).encode("utf-8"), headers_out)

    @staticmethod
    def _etag(path: str) -> str | None:
        """A stable, content-like ETag: only the intraday endpoint, like the real API."""
        if "/heart/" not in path:
            return None
        import hashlib

        return '"%s"' % hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]

    # --- routing ------------------------------------------------------------
    def respond(self, path: str) -> tuple[int, Any]:
        if path.endswith("/user/-/profile.json"):
            return 200, self.profile()
        parts = path.strip("/").split("/")
        # 1/user/-/<what>/date/<day>/<...>  (the day segment carries the ".json" suffix)
        try:
            day = date.fromisoformat(parts[parts.index("date") + 1].split(".")[0])
        except (ValueError, IndexError):
            return 404, {"errors": [{"errorType": "invalid_request", "message": f"unknown path {path}"}]}
        what = parts[parts.index("date") - 1]
        if "activities/heart" in path:
            return 200, self.heart_day(day)
        if what == "heart":
            if day == MISSING_HR_DAY:
                return 404, {"errors": [{"errorType": "not_found", "message": "No records found for that date"}]}
            return 200, self.heart(day)
        if what == "hrv":
            return 200, self.hrv(day)
        if what == "spo2":
            if day == MISSING_SPO2_DAY:
                return 404, {"errors": [{"errorType": "not_found", "message": "No SpO2 logs for that date"}]}
            return 200, self.spo2(day)
        if what == "sleep":
            return 200, self.sleep(day)
        if what == "activities":
            if day == MISSING_HR_DAY:
                return 404, {"errors": [{"errorType": "not_found", "message": "No records found for that date"}]}
            return 200, self.activity(day)
        return 404, {"errors": [{"errorType": "not_found", "message": f"unknown path {path}"}]}

    # --- bodies -------------------------------------------------------------
    def profile(self) -> dict[str, Any]:
        """``GET /1/user/-/profile.json`` - the account, and what it has resources for."""
        return {
            "user": {
                "displayName": "Sam Example",
                "firstName": "Sam",
                "lastName": "Example",
                "memberSinceDate": MEMBER_SINCE,
                "membershipLevel": "Premium",
                "locale": "en_US",
                "country": "IE",
                "timeZone": "UTC",
                "weightUnit": "METRIC",
                "heightUnit": "METRIC",
                "clockTimeDisplayFormat": 24,
                "age": 34,
                "gender": "male",
                "isEmployee": False,
                "isEmailVerified": True,
                "nonce": "REDACTED",
                "resources": list(RESOURCES),
                "encodedData": {"nickname": "REDACTED"},
            },
            "goals": {"steps": 10000, "veryActiveMinutes": 30, "calories": 2200},
        }

    def _stamp(self, day: date, hour: int = 0, minute: int = 0) -> str:
        """Fitbit's ``dateTime`` strings carry no offset - see ``[import] timezone``."""
        return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00.000"

    def heart(self, day: date) -> dict[str, Any]:
        """``GET /1/user/-/heart/date/D/1d/1min.json`` - intraday heart rate."""
        rng = random.Random(f"{SEED}:hr:{day.isoformat()}")
        resting = 55.0 + rng.uniform(-2.0, 2.0)
        points: list[dict[str, Any]] = []
        minutes = 0
        while minutes < 24 * 60:
            hour = minutes / 60
            base = resting + (0 if hour < 6 else 6 + 9 * min(1.0, max(0.0, (hour - 8) / 6)))
            if 17 <= hour < 18:
                base = 118 + rng.uniform(-8, 14)
            points.append(
                {
                    "dateTime": self._stamp(day, minutes // 60, minutes % 60),
                    "heartRate": round(min(190.0, max(38.0, base + rng.gauss(0, 2.4))), 1),
                }
            )
            minutes += HR_STEP_MINUTES
        return {"heart": points}

    def heart_day(self, day: date) -> dict[str, Any]:
        """``GET /1/user/-/activities/heart/date/D/1d.json`` - resting HR for the day."""
        rng = random.Random(f"{SEED}:resting:{day.isoformat()}")
        resting = round(54.0 + rng.uniform(-2.5, 3.5), 1)
        return {
            "activities-heart": [
                {
                    "dateTime": day.isoformat(),
                    "value": {
                        "customZoneMaximumHeartRate": 165,
                        "heartRateZones": [
                            {"name": "Out of Range", "minHeartRate": 40, "maxHeartRate": 95, "minutes": 320},
                            {"name": "Fat Burn", "minHeartRate": 95, "maxHeartRate": 124, "minutes": 118},
                            {"name": "Cardio", "minHeartRate": 124, "maxHeartRate": 145, "minutes": 21},
                        ],
                        "maxHeartRate": 176,
                        "minHeartRate": 43,
                        "restingHeartRate": resting,
                    },
                }
            ]
        }

    def activity(self, day: date) -> dict[str, Any]:
        """``GET /1/user/-/activities/date/D.json`` - the daily activity summary."""
        rng = random.Random(f"{SEED}:act:{day.isoformat()}")
        weekend = day.weekday() >= 5
        return {
            "activities": [],
            "goals": {"calories": 2200, "steps": 10000, "activelyMinutes": 30, "floors": 10, "distance": 0.0},
            "summary": {
                "activelyMinutes": rng.randrange(12, 96),
                "activePoints": rng.randrange(20, 120),
                "calories": float(rng.randrange(1850, 3100)),
                "caloriesBMR": float(rng.randrange(1450, 1800)),
                "distance": round(rng.uniform(3.0, 11.0), 3),
                "floors": rng.randrange(2, 28),
                "steps": rng.randrange(3200, 15200) + (2000 if weekend else 0),
                "sedentaryMinutes": rng.randrange(300, 700),
                "veryActiveMinutes": rng.randrange(0, 55),
                "fairlyActiveMinutes": rng.randrange(0, 30),
                "lightlyActiveMinutes": rng.randrange(60, 300),
            },
            "weekday": day.strftime("%A"),
        }

    def sleep(self, day: date) -> dict[str, Any]:
        """``GET /1/user/-/sleep/date/D.json`` - sleep logs, one (sometimes two) per day."""
        rng = random.Random(f"{SEED}:sleep:{day.isoformat()}")
        start = datetime(day.year, day.month, day.day, 23, rng.randrange(0, 50), tzinfo=UTC)
        logs = [
            {
                "logId": 1_000_000 + int(start.timestamp()),
                "isMainSleep": True,
                "dateOfSleep": day.isoformat(),
                "startTime": start.isoformat(timespec="milliseconds").replace("+00:00", ""),
                "endTime": (start + timedelta(hours=7, minutes=rng.randrange(0, 80)))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", ""),
                "minutesToFallAsleep": rng.randrange(1, 25),
                "minutesAsleep": rng.randrange(5 * 60 + 10, 8 * 60),
                "minutesAwake": rng.randrange(4, 60),
                "minutesAfterWakeup": rng.randrange(0, 25),
                "timeInBed": rng.randrange(6 * 60, 9 * 60),
                "awakeningsCount": rng.randrange(1, 12),
                "efficiency": rng.randrange(78, 99),
                "restingHeartRate": rng.randrange(49, 62),
                "minRestingHeartRate": rng.randrange(45, 52),
                "maxRestingHeartRate": rng.randrange(55, 66),
                "restingCalories": round(rng.uniform(48.0, 70.0), 2),
                "sleepStartTypeId": "Night",
                "duration": rng.randrange(5 * 60 + 10, 8 * 60) * 60_000,
            }
        ]
        if day.day % 7 == 3:  # a nap, so multi-log days are covered
            nap = datetime(day.year, day.month, day.day, 14, 10, tzinfo=UTC)
            logs.append(
                {
                    "logId": 2_000_000 + int(nap.timestamp()),
                    "isMainSleep": False,
                    "dateOfSleep": day.isoformat(),
                    "startTime": nap.isoformat(timespec="milliseconds").replace("+00:00", ""),
                    "endTime": (nap + timedelta(minutes=38)).isoformat(timespec="milliseconds").replace("+00:00", ""),
                    "minutesAsleep": 34,
                    "minutesAwake": 4,
                    "timeInBed": 38,
                    "efficiency": 89,
                    "restingHeartRate": 61,
                    "sleepStartTypeId": "Afternoon Nap",
                    "duration": 38 * 60_000,
                }
            )
        return {"sleep": logs, "pagination": {"index": 1, "size": len(logs), "limit": 100, "total": len(logs)}}

    def hrv(self, day: date) -> dict[str, Any]:
        """``GET /1/user/-/hrv/date/D/7d.json`` - a week of daily HRV, plus the
        per-minute and wake entries the importer must not mistake for daily values."""
        entries: list[dict[str, Any]] = []
        for offset in range(7):
            stamp = day + timedelta(days=offset)
            rng = random.Random(f"{SEED}:hrv:{stamp.isoformat()}")
            value = round(38.0 + rng.uniform(-9.0, 26.0), 2)
            entries.append(
                {
                    "type": "daily",
                    "dateTime": stamp.isoformat(),
                    "interval": "1d",
                    "hrvValue": value,
                    "hfTxt": None,
                    "percentile": rng.randrange(10, 95),
                    "supported": True,
                }
            )
            entries.append(
                {
                    "type": "min",
                    "dateTime": f"{stamp.isoformat()}T03:15:00.000",
                    "interval": "1d",
                    "hrvValue": round(value + rng.uniform(-6.0, 6.0), 2),
                    "supported": True,
                }
            )
        baseline = {"maxHrvValue": 62.0, "minHrvValue": 34.0, "unsupportedSleepSeconds": 0, "weightStatus": "OK"}
        return {"hrv": {"baseline": baseline, "heartRateVariabilityDd": entries}}

    def spo2(self, day: date) -> dict[str, Any]:
        """``GET /1/user/-/spo2/date/D/1d.json`` - nightly SpO2."""
        rng = random.Random(f"{SEED}:spo2:{day.isoformat()}")
        low = round(rng.uniform(88.5, 94.5), 1)
        average = round(low + rng.uniform(0.8, 3.2), 1)
        return {
            "dateTime": day.isoformat(),
            "summary": {"averageSPO2": average, "minSPO2": low},
            "spo2": [
                {
                    "startTime": self._stamp(day, 23, 30),
                    "endTime": self._stamp(day + timedelta(days=1), 6, 45),
                    "averageSPO2": average,
                    "minSPO2": low,
                    "maxSPO2": round(min(99.0, average + rng.uniform(0.5, 2.0)), 1),
                    "hasMainSleep": True,
                    "desaturationEvents": rng.randrange(0, 4),
                    "minutesSpo2Value": [
                        {"value": str(int(average)), "startTime": self._stamp(day, 23, 45)},
                        {"value": str(int(low)), "startTime": self._stamp(day, 23, 50)},
                    ],
                }
            ],
        }


def build(
    out: Path,
    *,
    days: int = DAYS,
    end: date = END_DAY,
    seed: int = SEED,
    datasets: tuple[str, ...] | None = None,
) -> list[str]:
    """Record every request a ``days``-day import of ``end`` makes. Returns the keys."""
    synthetic = SyntheticApi(start=end - timedelta(days=days - 1), stop=end, seed=seed)
    recording = RecordingTransport(synthetic, out, indent=None)
    client = FitbitClient(ApiSettings(), CredentialStore(Path(tempfile.mkdtemp()) / "credentials.json"),
                          import_settings=ImportSettings(), transport=recording)
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
                for path in chunk.paths():
                    try:
                        client.get(path)
                    except Exception:  # noqa: BLE001 - 404s are recorded, not raised past here
                        pass
        store.close()
    return recording.recorded


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
            changed = [name for name, path in sorted(recorded.items()) if not filecmp.cmp(path, before[name], shallow=False) if name in before]
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
