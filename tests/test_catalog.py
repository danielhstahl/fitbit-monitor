"""Tests for the dataset catalog: what each dataset asks for and how it is read.

The catalog is the only place that knows the Google Health API's endpoint shapes, so this
is where "we asked for the right thing and read it correctly" is pinned down: the verb
each data type is addressed with, the window and page size that keep a request inside the
API's own limits, the fields each response contributes, and the entries that must *not*
become rows.

The shapes are the published ``v4`` ones (the discovery document is the reference;
ADR-0002 §7 is the map being implemented), including the ones that are easiest to get
wrong: a rollup is a POST whose range is closed-open, a multi-word data type is kebab-case
in the path and snake_case in a filter, and a daily data type is read with ``list``
because rolling one up answers a different question entirely.
"""

from __future__ import annotations

import re
import unittest
from datetime import UTC, date, datetime, timedelta

from fitbit_monitor.api.catalog import (
    ACTIVITY_SCOPE,
    ALL_SCOPES,
    CATALOG,
    DAILY_ROLLUP_PAGE_MAX,
    DAILY_ROLLUP_PAGE_MAX_SHORT,
    HEALTH_METRICS_SCOPE,
    PAGE_MAX,
    SESSION_PAGE_MAX,
    SLEEP_SCOPE,
    Payload,
    daterange,
    lookup,
    permitted_datasets,
    resolve_names,
    short_scope,
)
from fitbit_monitor.errors import UsageError
from fitbit_monitor.timeutil import DateRange, parse_date, parse_timestamp, resolve_timezone

START = date(2026, 5, 1)
STOP = date(2026, 5, 7)


def body_of(dataset: str, start: date = START, stop: date = STOP) -> list[dict]:
    """The JSON bodies of one chunk's requests, for shape assertions."""
    return [request.json_body() or {} for request in CATALOG[dataset].requests_in_chunk(start, stop)]


def requests_of(start: date, stop: date) -> list:
    return list(CATALOG["heart_rate"].requests_in_chunk(start, stop))


class ScopeTest(unittest.TestCase):
    def test_the_scope_set_is_the_four_readonly_googlehealth_scopes(self):
        self.assertEqual(len(ALL_SCOPES), 4)
        for scope in ALL_SCOPES:
            self.assertTrue(scope.startswith("https://www.googleapis.com/auth/googlehealth."), scope)
            self.assertTrue(scope.endswith(".readonly"), scope)

    def test_no_write_scope_is_asked_for_and_no_clinical_one_either(self):
        # ADR-0002 §4: ecg and irn are relevant to the arrhythmia epic and deliberately
        # not asked for until a plugin consumes them.
        self.assertFalse([s for s in ALL_SCOPES if ".writeonly" in s])
        self.assertFalse([s for s in ALL_SCOPES if ".ecg" in s or ".irn" in s])

    def test_every_dataset_names_only_scopes_that_are_asked_for_at_login(self):
        for name, dataset in CATALOG.items():
            with self.subTest(dataset=name):
                self.assertTrue(dataset.scopes)
                for scope in dataset.scopes:
                    self.assertIn(scope, ALL_SCOPES)

    def test_short_scope_is_the_part_a_human_reads(self):
        self.assertEqual(short_scope(SLEEP_SCOPE), "sleep.readonly")
        self.assertEqual(short_scope("something-else"), "something-else")


class DatasetDefinitionTest(unittest.TestCase):
    def test_every_dataset_declares_the_knowledge_the_importer_needs(self):
        for name, dataset in CATALOG.items():
            with self.subTest(dataset=name):
                self.assertTrue(dataset.summary)
                self.assertGreaterEqual(dataset.chunk_days, 1)
                requests = list(dataset.requests_in_chunk(START, START))
                self.assertTrue(requests)
                for request in requests:
                    self.assertTrue(request.path().startswith("/users/me/dataTypes/"))
                    self.assertTrue(request.items in ("dataPoints", "rollupDataPoints"))

    def test_heart_rate_is_a_60_second_rollup_post_and_one_request_per_six_days(self):
        # Six days of per-minute buckets is 8 640 points: the largest window that still
        # comes back in one page, which is why the window and the page size are decided
        # together (ADR-0002 §6).
        (only,) = CATALOG["heart_rate"].requests_in_chunk(START, START + timedelta(days=5))
        self.assertEqual(only.method, "POST")
        twelve_days = CATALOG["heart_rate"].requests_in_chunk(START, START + timedelta(days=11))
        self.assertEqual(len(twelve_days), 2)
        first = twelve_days[0]
        self.assertEqual(first.method, "POST")
        self.assertEqual(first.path(), "/users/me/dataTypes/heart-rate/dataPoints:rollUp")
        self.assertEqual(first.items, "rollupDataPoints")
        body = first.json_body() or {}
        self.assertEqual(body["windowSize"], "60s")
        self.assertEqual(body["pageSize"], PAGE_MAX)

    def test_a_rollup_range_is_closed_open_so_it_ends_at_the_midnight_after(self):
        body = body_of("heart_rate", START, STOP)[0]
        self.assertEqual(body["range"]["startTime"], "2026-05-01T00:00:00Z")
        self.assertEqual(body["range"]["endTime"], "2026-05-07T00:00:00Z")

    def test_a_window_never_exceeds_the_api_s_own_range_cap(self):
        # A user who sets [import] chunk_days = 30 gets several requests, not a 400 from
        # a rollup range the service refuses: the catalog absorbs the difference between
        # what the config asks for and what the endpoint allows.
        for name in sorted(CATALOG):
            with self.subTest(dataset=name):
                stop = START + timedelta(days=89)
                requests = CATALOG[name].requests_in_chunk(START, stop)
                self.assertTrue(requests)
                covered: set[date] = set()
                for request in requests:
                    span = (max(request.covers) - min(request.covers)).days + 1
                    self.assertLessEqual(span, 90, request.path())
                    covered.update(request.covers)
                # Splitting a window must not lose a day of it or ask for one twice: a
                # hole here is a silent gap in the store, not an error anyone sees.
                self.assertEqual(covered, set(daterange(START, stop)))

    def test_the_page_estimate_is_part_of_the_cost_because_a_page_is_a_request(self):
        # A 14-day 60-second window is 20 160 points against a 10 000-point page: three
        # calls. A plan that says "one" under-books the import it is promising.
        heart = CATALOG["heart_rate"]
        self.assertEqual(heart.request_cost(START, START + timedelta(days=13)), 3)
        self.assertEqual(heart.request_cost(START, START + timedelta(days=5)), 1)

    def test_daily_summary_is_four_daily_rollups_plus_resting_hr_read_raw(self):
        requests = CATALOG["daily_summary"].requests_in_chunk(START, STOP)
        verbs = [(r.method, r.path()) for r in requests]
        self.assertEqual(
            verbs,
            [
                ("POST", "/users/me/dataTypes/steps/dataPoints:dailyRollUp"),
                ("POST", "/users/me/dataTypes/active-minutes/dataPoints:dailyRollUp"),
                ("POST", "/users/me/dataTypes/total-calories/dataPoints:dailyRollUp"),
                ("POST", "/users/me/dataTypes/floors/dataPoints:dailyRollUp"),
                ("GET", "/users/me/dataTypes/daily-resting-heart-rate/dataPoints"),
            ],
        )
        # The last one is a `list`, not a rollup, on purpose: dailyRollUp on a daily type
        # answers with the personal range, not the day's beats per minute.
        self.assertIsNone(requests[-1].json_body())

    def test_a_daily_rollup_range_is_civil_and_ends_the_day_after(self):
        rollup = body_of("daily_summary", START, STOP)[0]
        self.assertEqual(rollup["windowSizeDays"], 1)
        self.assertEqual(rollup["range"]["start"], {"date": {"year": 2026, "month": 5, "day": 1}})
        self.assertEqual(rollup["range"]["end"], {"date": {"year": 2026, "month": 5, "day": 8}})

    def test_daily_summary_needs_both_of_the_scopes_it_pulls_from(self):
        self.assertEqual(set(CATALOG["daily_summary"].scopes), {HEALTH_METRICS_SCOPE, ACTIVITY_SCOPE})

    def test_sleep_is_a_page_of_25_filtered_on_civil_end_time(self):
        (request,) = CATALOG["sleep"].requests_in_chunk(START, STOP)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path(), "/users/me/dataTypes/sleep/dataPoints")
        params = request.params()
        self.assertEqual(params["pageSize"], str(SESSION_PAGE_MAX))
        # Half-open, and a bare civil date is already midnight: seven nights, no clock
        # time to get wrong and no last second of the final day outside the window.
        self.assertEqual(
            params["filter"],
            'sleep.interval.civil_end_time >= "2026-05-01" AND sleep.interval.civil_end_time < "2026-05-08"',
        )

    def test_a_daily_list_filter_ends_the_day_after_the_last_one_covered(self):
        (request,) = CATALOG["hrv"].requests_in_chunk(START, STOP)
        self.assertEqual(
            request.params()["filter"],
            'daily_heart_rate_variability.date >= "2026-05-01" AND daily_heart_rate_variability.date < "2026-05-08"',
        )

    def test_no_filter_uses_a_comparator_the_grammar_does_not_have(self):
        """v4 has two comparators, ``>=`` and ``<``, and no ``<=``.

        "Supported comparison operators: ``>=``, ``<``" - the ``filter`` parameter of
        ``GET /v4/{parent}/dataPoints`` in the published discovery document. The service
        checks this per data type, so one wrong comparator does not break the import: it
        turns one dataset into ``400 INVALID_ARGUMENT /
        INVALID_DATA_POINT_FILTER_RESTRICTION_COMPARATOR``, which lands as a warning line
        under an otherwise successful run. Sweeping every dataset's every request is what
        stops one definition being quietly wrong.
        """
        comparators = re.compile(r"(<=|>=|<|>)")
        for name, dataset in sorted(CATALOG.items()):
            for request in dataset.requests_in_chunk(START, STOP):
                with self.subTest(dataset=name, path=request.path()):
                    for found in comparators.findall(request.params().get("filter", "")):
                        self.assertIn(found, (">=", "<"), f"{name} sent {found!r}, which v4 refuses")

    def test_a_daily_rollup_asks_for_a_page_size_the_data_type_allows(self):
        """The page cap is per verb *and* per data type: 90, or 14 for four of them.

        Asking for 10 000 when the documented maximum is 90 works only while the server is
        lenient about it, and "lenient" is not something to bank on; 14 daily points is
        also exactly the largest short-range window this file builds.
        """
        sizes = {}
        for request in CATALOG["daily_summary"].requests_in_chunk(START, STOP):
            sizes[request.path().split("/dataTypes/")[1].split("/")[0]] = (request.json_body() or {}).get("pageSize")
        self.assertEqual(sizes["steps"], DAILY_ROLLUP_PAGE_MAX)
        self.assertEqual(sizes["floors"], DAILY_ROLLUP_PAGE_MAX)
        self.assertEqual(sizes["active-minutes"], DAILY_ROLLUP_PAGE_MAX_SHORT)
        self.assertEqual(sizes["total-calories"], DAILY_ROLLUP_PAGE_MAX_SHORT)
        # ... and the window field is the one *this* verb has: `dailyRollUp` takes
        # `windowSizeDays`, `rollUp` takes `windowSize`. An unknown field in a POST body is
        # another 400 waiting to happen.
        for request in CATALOG["daily_summary"].requests_in_chunk(START, STOP):
            body = request.json_body()
            if body is None:
                continue  # the resting-HR half of this dataset is a `list`, not a rollup
            self.assertEqual(body.get("windowSizeDays"), 1)
            self.assertNotIn("windowSize", body)

    def test_daily_types_are_listed_with_a_snake_case_filter_field(self):
        for name, field in (
            ("hrv", "daily_heart_rate_variability.date"),
            ("spo2", "daily_oxygen_saturation.date"),
        ):
            with self.subTest(dataset=name):
                (request,) = CATALOG[name].requests_in_chunk(START, STOP)
                self.assertIn(field, request.params()["filter"])
                self.assertNotIn("-", request.params()["filter"].split(" ")[0])

    def test_sleep_rows_cover_every_day_asked_and_a_chunk_is_one_page(self):
        (request,) = CATALOG["sleep"].requests_in_chunk(START, STOP)
        self.assertEqual(request.covers, tuple(daterange(START, STOP)))
        self.assertEqual(request.pages, 1)


class RequestPagingTest(unittest.TestCase):
    def test_a_post_carries_its_page_token_in_the_body(self):
        (request,) = CATALOG["heart_rate"].requests_in_chunk(START, START + timedelta(days=5))
        page2 = request.next_page("abc")
        self.assertEqual(page2.json_body()["pageToken"], "abc")
        self.assertEqual(page2.path(), request.path())
        self.assertEqual(page2.covers, request.covers)

    def test_a_get_carries_its_page_token_in_the_query(self):
        (request,) = CATALOG["spo2"].requests_in_chunk(START, STOP)
        page2 = request.next_page("xyz")
        self.assertEqual(page2.params()["pageToken"], "xyz")
        self.assertIsNone(page2.json_body())

    def test_no_token_means_no_next_page(self):
        (request,) = CATALOG["spo2"].requests_in_chunk(START, STOP)
        for body in ({}, {"nextPageToken": ""}, {"nextPageToken": "   "}):
            self.assertIsNone(request.page_token(body))
        self.assertEqual(request.page_token({"nextPageToken": "t"}), "t")


class LookupTest(unittest.TestCase):
    def test_unknown_dataset_names_the_valid_ones(self):
        with self.assertRaises(UsageError) as caught:
            lookup("glucose")
        self.assertIn("heart_rate", str(caught.exception.hint))
        self.assertEqual(caught.exception.exit_code, 2)

    def test_resolve_names_accepts_commas_and_repeats(self):
        self.assertEqual(resolve_names(["heart_rate, sleep", "heart_rate"]), ("heart_rate", "sleep"))

    def test_resolve_names_defaults_to_the_whole_catalog(self):
        self.assertEqual(resolve_names(None), tuple(sorted(CATALOG)))

    def test_resolve_names_honours_the_allowed_list(self):
        self.assertEqual(resolve_names(None, allowed=["sleep", "heart_rate"]), ("heart_rate", "sleep"))

    def test_resolve_names_refuses_an_empty_selection(self):
        with self.assertRaises(UsageError):
            resolve_names([",,"])
        with self.assertRaises(UsageError):
            resolve_names(None, allowed=["nothing"])


class PermissionTest(unittest.TestCase):
    def test_a_token_with_all_four_scopes_imports_everything(self):
        allowed, skipped = permitted_datasets({}, ALL_SCOPES)
        self.assertEqual(allowed, ("daily_summary", "heart_rate", "hrv", "sleep", "spo2"))
        self.assertEqual(skipped, ())

    def test_a_missing_scope_skips_the_dataset_and_names_the_scope(self):
        allowed, skipped = permitted_datasets({}, [HEALTH_METRICS_SCOPE])
        self.assertEqual(allowed, ("heart_rate", "hrv", "spo2"))
        reasons = dict(item.split(": ", 1) for item in skipped)
        self.assertIn("sleep.readonly", reasons["sleep"])
        self.assertIn("activity_and_fitness.readonly", reasons["daily_summary"])

    def test_partial_consent_degrades_with_a_reason_rather_than_a_crash(self):
        # Google's own rule for a withheld scope is that the app keeps working; naming the
        # gap is what makes that diagnosable instead of a silent short import. Four of the
        # five datasets read vitals, so granting only activity_and_fitness imports nothing
        # - and says so five times rather than failing.
        allowed, skipped = permitted_datasets({}, [ACTIVITY_SCOPE])
        self.assertEqual(allowed, ())
        self.assertEqual(len(skipped), 5)
        for item in skipped:
            self.assertIn("scope", item)

    def test_an_empty_grant_list_assumes_nothing_and_grants_everything(self):
        allowed, skipped = permitted_datasets({}, [])
        self.assertEqual(len(allowed), len(CATALOG))
        self.assertEqual(skipped, ())

    def test_a_profile_vetoes_nothing_because_v4_publishes_no_resource_list(self):
        allowed, _ = permitted_datasets({"age": 34, "membershipStartDate": {"year": 2022}}, ALL_SCOPES[:1])
        self.assertEqual(allowed, ("heart_rate", "hrv", "spo2"))


class NormalizerTest(unittest.TestCase):
    def test_a_rollup_bucket_becomes_hr_plus_its_min_and_max(self):
        body = {
            "rollupDataPoints": [
                {
                    "startTime": "2026-05-01T07:05:00Z",
                    "endTime": "2026-05-01T07:06:00Z",
                    "heartRate": {"beatsPerMinuteAvg": 61.0, "beatsPerMinuteMin": 58.0, "beatsPerMinuteMax": 66.0},
                },
                {"heartRate": {"beatsPerMinuteAvg": 61.0}},  # no window - not a row
                "not-a-dict",
            ]
        }
        rows = CATALOG["heart_rate"].normalize([Payload(day=START, body=body)], "UTC")
        self.assertEqual(
            rows,
            [(datetime(2026, 5, 1, 7, 5, tzinfo=UTC), {"hr": 61.0, "hr_min": 58.0, "hr_max": 66.0})],
        )

    def test_a_physical_timestamp_is_absolute_and_a_naive_one_uses_the_account_timezone(self):
        body = {"rollupDataPoints": [{"startTime": "2026-05-01T07:05:00Z", "heartRate": {"beatsPerMinuteAvg": 61}}]}
        self.assertEqual(
            CATALOG["heart_rate"].normalize([body], "Europe/Berlin")[0][0],
            datetime(2026, 5, 1, 7, 5, tzinfo=UTC),
        )
        naive = {"rollupDataPoints": [{"startTime": "2026-05-01T07:05:00", "heartRate": {"beatsPerMinuteAvg": 61}}]}
        self.assertEqual(
            CATALOG["heart_rate"].normalize([naive], "Europe/Berlin")[0][0],
            datetime(2026, 5, 1, 5, 5, tzinfo=UTC),
        )

    def test_daily_summary_merges_the_rollups_and_the_resting_hr_list_onto_one_instant(self):
        payloads = [
            {
                "rollupDataPoints": [
                    {
                        "civilStartTime": {"date": {"year": 2026, "month": 5, "day": 1}},
                        "steps": {"countSum": "9000"},
                        "totalCalories": {"kcalSum": 2400.5},
                        "floors": {"countSum": "12"},
                        "activeMinutes": {
                            "activeMinutesRollupByActivityLevel": [
                                {"activityLevel": "SlightlyActive", "minutes": "30"},
                                {"activityLevel": "VeryActive", "minutes": "12"},
                            ]
                        },
                    }
                ]
            },
            {
                "dataPoints": [
                    {
                        "dailyRestingHeartRate": {
                            "date": {"year": 2026, "month": 5, "day": 1},
                            "beatsPerMinute": "55",
                        }
                    }
                ]
            },
        ]
        rows = CATALOG["daily_summary"].normalize(payloads, "UTC")
        self.assertEqual([ts for ts, _ in rows], [datetime(2026, 5, 1, tzinfo=UTC)] * 2)
        self.assertEqual(rows[0][1], {"steps": 9000.0, "calories": 2400.5, "floors": 12.0, "active_minutes": 42.0})
        self.assertEqual(rows[1][1], {"resting_hr": 55.0})

    def test_daily_summary_ignores_bodies_with_no_numbers(self):
        payloads = [
            {"rollupDataPoints": [{"civilStartTime": {"date": {"year": 2026, "month": 5, "day": 1}}}]},
            {"dataPoints": [{"dailyRestingHeartRate": {"date": {}}}]},
        ]
        self.assertEqual(CATALOG["daily_summary"].normalize(payloads, "UTC"), [])

    def test_a_day_answered_by_a_rollup_without_a_civil_start_is_not_a_row(self):
        self.assertEqual(
            CATALOG["daily_summary"].normalize([{"rollupDataPoints": [{"steps": {"countSum": "1"}}]}], "UTC"),
            [],
        )

    def test_sleep_one_row_per_session_with_stage_minutes(self):
        body = {
            "dataPoints": [
                {
                    "sleep": {
                        "interval": {
                            "startTime": "2026-05-01T23:05:00Z",
                            "endTime": "2026-05-02T06:45:00Z",
                            "civilEndTime": {"date": {"year": 2026, "month": 5, "day": 2}},
                        },
                        "summary": {
                            "minutesAsleep": "425",
                            "minutesAwake": "30",
                            "minutesInSleepPeriod": "455",
                            "stagesSummary": [
                                {"type": "AWAKE", "count": "4", "minutes": "30"},
                                {"type": "DEEP", "count": "2", "minutes": "84"},
                                {"type": "REM", "count": "3", "minutes": "96"},
                                {"type": "LIGHT", "count": "5", "minutes": "245"},
                            ],
                        },
                    }
                },
                {"sleep": {"summary": {"minutesAsleep": "10"}}},  # no interval at all - not a row
                "not-a-dict",
            ]
        }
        rows = CATALOG["sleep"].normalize([Payload(day=START, body=body)], "UTC")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], datetime(2026, 5, 1, 23, 5, tzinfo=UTC))
        self.assertEqual(
            rows[0][1],
            {
                "sleep_minutes": 425.0,
                "awake_minutes": 30.0,
                "time_in_bed_minutes": 455.0,
                "deep_minutes": 84.0,
                "rem_minutes": 96.0,
                "light_minutes": 245.0,
                "awakenings": 4.0,
                "efficiency": 93.4,
            },
        )

    def test_hrv_daily_point_becomes_rmssd(self):
        body = {
            "dataPoints": [
                {
                    "dailyHeartRateVariability": {
                        "date": {"year": 2026, "month": 5, "day": 1},
                        "averageHeartRateVariabilityMilliseconds": 44.0,
                        "entropy": 2.9,
                    }
                },
                {"dailyHeartRateVariability": {"averageHeartRateVariabilityMilliseconds": 30.0}},  # no date
            ]
        }
        self.assertEqual(
            CATALOG["hrv"].normalize([Payload(day=START, body=body)], "UTC"),
            [(datetime(2026, 5, 1, tzinfo=UTC), {"rmssd": 44.0, "entropy": 2.9})],
        )

    def test_spo2_average_and_confidence_bounds(self):
        body = {
            "dataPoints": [
                {
                    "dailyOxygenSaturation": {
                        "date": {"year": 2026, "month": 5, "day": 1},
                        "averagePercentage": 95.5,
                        "lowerBoundPercentage": 93.0,
                        "upperBoundPercentage": 97.0,
                    }
                }
            ]
        }
        rows = CATALOG["spo2"].normalize([Payload(day=START, body=body)], "UTC")
        self.assertEqual(rows[0][0], datetime(2026, 5, 1, tzinfo=UTC))
        self.assertEqual(rows[0][1], {"spo2_avg": 95.5, "spo2_min": 93.0, "spo2_max": 97.0})

    def test_int64_fields_arrive_as_strings_and_are_still_numbers(self):
        body = {"rollupDataPoints": [{"startTime": "2026-05-01T07:05:00Z", "heartRate": {"beatsPerMinuteAvg": "61"}}]}
        self.assertEqual(CATALOG["heart_rate"].normalize([body], "UTC")[0][1]["hr"], 61.0)

    def test_a_value_that_is_not_a_number_is_dropped_not_raised(self):
        body = {
            "rollupDataPoints": [
                {"startTime": "2026-05-01T07:05:00Z", "heartRate": {"beatsPerMinuteAvg": "n/a", "beatsPerMinuteMin": 58}}
            ]
        }
        self.assertEqual(CATALOG["heart_rate"].normalize([body], "UTC")[0][1], {"hr_min": 58.0})

    def test_bare_dicts_are_accepted_too(self):
        self.assertTrue(CATALOG["spo2"].normalize([{"dataPoints": []}], "UTC") == [])


class TimeutilTest(unittest.TestCase):
    def test_parse_timestamp_forms(self):
        self.assertEqual(parse_timestamp("2026-05-01T07:05:00.000"), datetime(2026, 5, 1, 7, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("2026-05-01 07:05:00"), datetime(2026, 5, 1, 7, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("05/01/2026 07:05", fmt="%m/%d/%Y %H:%M"), datetime(2026, 5, 1, 7, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("2026-05-01T07:05:00+02:00"), datetime(2026, 5, 1, 5, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("2026-05-01T07:05:00Z"), datetime(2026, 5, 1, 7, 5, tzinfo=UTC))
        self.assertEqual(parse_timestamp("2026-05-01", tz_name="Europe/Berlin"), datetime(2026, 4, 30, 22, tzinfo=UTC))

    def test_parse_timestamp_rejects_junk_with_a_field_name(self):
        with self.assertRaises(UsageError) as caught:
            parse_timestamp("yesterday-ish", field="startTime")
        self.assertIn("startTime", str(caught.exception))

    def test_resolve_timezone_names_the_setting(self):
        self.assertEqual(resolve_timezone("UTC"), UTC)
        with self.assertRaises(UsageError) as caught:
            resolve_timezone("Mars/Olympus")
        self.assertIn("[import] timezone", str(caught.exception.hint))

    def test_parse_date_forms(self):
        self.assertEqual(parse_date("2026-05-01"), date(2026, 5, 1))
        self.assertEqual(parse_date("05/01/2026"), date(2026, 5, 1))
        with self.assertRaises(UsageError):
            parse_date("nope")

    def test_date_range_still_validates(self):
        with self.assertRaises(UsageError):
            DateRange(since=datetime(2026, 5, 5, tzinfo=UTC), until=datetime(2026, 5, 1, tzinfo=UTC))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
