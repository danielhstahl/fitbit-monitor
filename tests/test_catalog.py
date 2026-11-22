"""Tests for the dataset catalog: what each dataset asks for and how it is read.

The catalog is the only place that knows Fitbit's endpoint shapes, so this is where
"we asked for the right thing and read it correctly" is pinned down: per-day versus
per-chunk request cost, the fields each response contributes, and the entries that must
*not* become rows (HRV's per-minute and wake samples, a sleep log with no numbers).
"""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from fitbit_monitor.api.catalog import (
    ALL_SCOPES,
    CATALOG,
    Payload,
    daterange,
    lookup,
    permitted_datasets,
    resolve_names,
)
from fitbit_monitor.errors import UsageError
from fitbit_monitor.timeutil import DateRange, parse_date, parse_timestamp, resolve_timezone

START = date(2026, 5, 1)
STOP = date(2026, 5, 7)


class DatasetDefinitionTest(unittest.TestCase):
    def test_every_dataset_declares_the_knowledge_the_importer_needs(self) -> None:
        for name, dataset in CATALOG.items():
            with self.subTest(dataset=name):
                self.assertTrue(dataset.summary)
                self.assertTrue(dataset.scope)
                self.assertGreaterEqual(dataset.chunk_days, 1)
                self.assertTrue(list(dataset.requests_in_chunk(START, START)))

    def test_intraday_heart_rate_costs_one_request_per_day(self) -> None:
        requests = CATALOG["heart_rate"].requests_in_chunk(START, STOP)
        self.assertEqual(len(requests), 7)
        self.assertEqual(requests[0].path(), "/1/user/-/heart/date/2026-05-01/1d/1min.json")

    def test_hrv_costs_one_request_per_chunk_because_the_window_is_seven_days(self) -> None:
        requests = CATALOG["hrv"].requests_in_chunk(START, STOP)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].path(), "/1/user/-/hrv/date/2026-05-01/7d.json")
        self.assertEqual(requests[0].covers, tuple(daterange(START, STOP)))

    def test_daily_summary_is_two_endpoints_a_day_merged_into_one_dataset(self) -> None:
        requests = CATALOG["daily_summary"].requests_in_chunk(START, date(2026, 5, 2))
        self.assertEqual(
            [request.path() for request in requests],
            [
                "/1/user/-/activities/date/2026-05-01.json",
                "/1/user/-/activities/heart/date/2026-05-01/1d.json",
                "/1/user/-/activities/date/2026-05-02.json",
                "/1/user/-/activities/heart/date/2026-05-02/1d.json",
            ],
        )
        self.assertEqual(CATALOG["daily_summary"].request_cost(START, date(2026, 5, 2)), 4)

    def test_sleep_and_spo2_are_per_day(self) -> None:
        self.assertEqual(CATALOG["sleep"].requests_in_chunk(START, START)[0].path(), "/1/user/-/sleep/date/2026-05-01.json")
        self.assertEqual(CATALOG["spo2"].requests_in_chunk(START, START)[0].path(), "/1/user/-/spo2/date/2026-05-01/1d.json")

    def test_the_scopes_the_catalog_needs_are_the_scopes_login_asks_for(self) -> None:
        self.assertIn("heartbeat", ALL_SCOPES)
        self.assertIn("spo2", ALL_SCOPES)
        for dataset in CATALOG.values():
            self.assertIn(dataset.scope, ALL_SCOPES)


class LookupTest(unittest.TestCase):
    def test_unknown_dataset_names_the_valid_ones(self) -> None:
        with self.assertRaises(UsageError) as caught:
            lookup("glucose")
        self.assertIn("heart_rate", str(caught.exception.hint))
        self.assertEqual(caught.exception.exit_code, 2)

    def test_resolve_names_accepts_commas_and_repeats(self) -> None:
        self.assertEqual(resolve_names(["heart_rate, sleep", "heart_rate"]), ("heart_rate", "sleep"))

    def test_resolve_names_defaults_to_the_whole_catalog(self) -> None:
        self.assertEqual(resolve_names(None), tuple(sorted(CATALOG)))

    def test_resolve_names_honours_the_allowed_list(self) -> None:
        self.assertEqual(resolve_names(None, allowed=["sleep", "heart_rate"]), ("heart_rate", "sleep"))

    def test_resolve_names_refuses_an_empty_selection(self) -> None:
        with self.assertRaises(UsageError):
            resolve_names([",,"])
        with self.assertRaises(UsageError):
            resolve_names(None, allowed=["nothing"])


class PermissionTest(unittest.TestCase):
    PROFILE = {"resources": ["profile", "activities", "sleep", "heartRate"]}

    def test_scope_and_resource_both_get_a_veto(self) -> None:
        allowed, skipped = permitted_datasets(self.PROFILE, ["profile", "activity", "sleep", "heartbeat"])
        self.assertEqual(allowed, ("daily_summary", "heart_rate", "sleep"))
        self.assertEqual(len(skipped), 2)
        self.assertTrue(any("spo2" in reason for reason in skipped))

    def test_a_token_with_every_scope_imports_everything_the_account_has(self) -> None:
        allowed, skipped = permitted_datasets(self.PROFILE, ALL_SCOPES)
        self.assertEqual(allowed, ("daily_summary", "heart_rate", "hrv", "sleep", "spo2"))
        self.assertEqual(skipped, ())

    def test_an_empty_profile_assumes_nothing_and_grants_by_scope(self) -> None:
        allowed, _ = permitted_datasets({}, ["activity"])
        self.assertEqual(allowed, ("daily_summary",))

    def test_skips_are_explained_not_silent(self) -> None:
        _, skipped = permitted_datasets({"resources": ["profile"]}, ["profile", "heartbeat"])
        self.assertTrue(any("heartRate" in reason for reason in skipped))


class NormalizerTest(unittest.TestCase):
    def test_intraday_heart_rate_rows(self) -> None:
        body = {"heart": [{"dateTime": "2026-05-01T07:05:00.000", "heartRate": 61}, {"dateTime": "nope", "heartRate": 61}]}
        rows = CATALOG["heart_rate"].normalize([Payload(day=START, body=body)], "UTC")
        self.assertEqual(rows, [(datetime(2026, 5, 1, 7, 5, tzinfo=UTC), {"hr": 61.0})])

    def test_timestamps_are_local_until_told_otherwise(self) -> None:
        body = {"heart": [{"dateTime": "2026-05-01T07:05:00.000", "heartRate": 61}]}
        rows = CATALOG["heart_rate"].normalize([Payload(day=START, body=body)], "Europe/Berlin")
        self.assertEqual(rows[0][0], datetime(2026, 5, 1, 5, 5, tzinfo=UTC))

    def test_daily_summary_merges_both_endpoints_onto_one_instant(self) -> None:
        payloads = [
            Payload(day=START, body={"summary": {"steps": 9000, "calories": 2400.5, "activelyMinutes": 42}}),
            Payload(day=START, body={"activities-heart": [{"dateTime": "2026-05-01", "value": {"restingHeartRate": 55.0}}]}),
        ]
        rows = CATALOG["daily_summary"].normalize(payloads, "UTC")
        self.assertEqual([ts for ts, _ in rows], [datetime(2026, 5, 1, tzinfo=UTC)] * 2)
        self.assertEqual(rows[0][1]["steps"], 9000.0)
        self.assertEqual(rows[1][1]["resting_hr"], 55.0)

    def test_daily_summary_ignores_bodies_with_no_numbers(self) -> None:
        payloads = [Payload(day=START, body={"summary": {}, "activities-heart": [{"value": {}}]})]
        self.assertEqual(CATALOG["daily_summary"].normalize(payloads, "UTC"), [])

    def test_hrv_keeps_only_the_daily_entries(self) -> None:
        body = {
            "hrv": {
                "heartRateVariabilityDd": [
                    {"type": "daily", "dateTime": "2026-05-01", "hrvValue": 44.0},
                    {"type": "min", "dateTime": "2026-05-01T03:00:00.000", "hrvValue": 30.0},
                    {"type": "wake", "dateTime": "2026-05-01T07:00:00.000", "hrvValue": 25.0},
                    {"type": "daily", "dateTime": "2026-05-02", "hrvValue": 51.0, "sdnn": 40.0},
                ]
            }
        }
        rows = CATALOG["hrv"].normalize([Payload(day=START, body=body)], "UTC")
        self.assertEqual(
            rows,
            [
                (datetime(2026, 5, 1, tzinfo=UTC), {"rmssd": 44.0}),
                (datetime(2026, 5, 2, tzinfo=UTC), {"rmssd": 51.0, "sdnn": 40.0}),
            ],
        )

    def test_sleep_one_row_per_log(self) -> None:
        body = {
            "sleep": [
                {
                    "startTime": "2026-05-01T23:05:00.000",
                    "minutesAsleep": 425,
                    "minutesAwake": 30,
                    "minutesDeep": 84,
                    "efficiency": 93,
                },
                {"startTime": "2026-05-02T14:00:00.000", "minutesAsleep": 34},
                {"minutesAsleep": 10},  # no timestamp at all - not a row
                "not-a-dict",
            ]
        }
        rows = CATALOG["sleep"].normalize([Payload(day=START, body=body)], "UTC")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], {"sleep_minutes": 425.0, "awake_minutes": 30.0, "deep_minutes": 84.0, "efficiency": 93.0})

    def test_spo2_rows(self) -> None:
        body = {"spo2": [{"startTime": "2026-05-01T23:30:00.000", "averageSPO2": 95.5, "minSPO2": 91.0, "maxSPO2": 98.0}]}
        rows = CATALOG["spo2"].normalize([Payload(day=START, body=body)], "UTC")
        self.assertEqual(rows[0][1], {"spo2_avg": 95.5, "spo2_min": 91.0, "spo2_max": 98.0})

    def test_bare_dicts_are_accepted_too(self) -> None:
        self.assertTrue(CATALOG["spo2"].normalize([{"spo2": [{"startTime": "2026-05-01T23:30:00.000", "averageSPO2": 95}]}], "UTC"))


class TimeutilTest(unittest.TestCase):
    def test_parse_timestamp_forms(self) -> None:
        self.assertEqual(parse_timestamp("2026-05-01T07:05:00.000"), datetime(2026, 5, 1, 7, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("2026/05/01 07:05:00"), datetime(2026, 5, 1, 7, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("05/01/2026 07:05", fmt="%m/%d/%Y %H:%M"), datetime(2026, 5, 1, 7, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("2026-05-01T07:05:00+02:00"), datetime(2026, 5, 1, 5, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("2026-05-01T07:05:00Z"), datetime(2026, 5, 1, 7, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("2026-05-01", tz_name="Europe/Berlin"), datetime(2026, 4, 30, 22, tzinfo=UTC))

    def test_parse_timestamp_rejects_junk_with_a_field_name(self) -> None:
        with self.assertRaises(UsageError) as caught:
            parse_timestamp("yesterday-ish", field="startTime")
        self.assertIn("startTime", str(caught.exception))

    def test_resolve_timezone_names_the_setting(self) -> None:
        self.assertEqual(resolve_timezone("UTC"), UTC)
        with self.assertRaises(UsageError) as caught:
            resolve_timezone("Mars/Olympus")
        self.assertIn("[import] timezone", str(caught.exception.hint))

    def test_parse_date_forms(self) -> None:
        self.assertEqual(parse_date("2026-05-01"), date(2026, 5, 1))
        self.assertEqual(parse_date("2026/05/01"), date(2026, 5, 1))
        self.assertEqual(parse_date("05/01/2026"), date(2026, 5, 1))
        with self.assertRaises(UsageError):
            parse_date("nope")

    def test_date_range_still_validates(self) -> None:
        with self.assertRaises(UsageError):
            DateRange(since=datetime(2026, 5, 5, tzinfo=UTC), until=datetime(2026, 5, 1, tzinfo=UTC))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
