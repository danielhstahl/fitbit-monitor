"""``fm api probe``: the diagnosis command, exercised entirely against the fixtures.

The whole point of the command is to answer "why did it import nothing" without a
network, a secret or a stack trace, so everything here runs on the recorded fixture set
(``tests/fixtures/api``) plus small hand-written records patched into it:

* granted scopes against the scopes the catalog wants, naming each gap and what it disables;
* a dataset answering with data, with ``403``, with ``404``, with a body this build does
  not recognise, or with nothing at all - each one reported on its own line;
* the rate-limit headers as sent, parsed and verbatim;
* the earliest date of available data, and *how* well that is bounded;
* the request budget, the exit codes, and ``--json`` for scripts and ``fm doctor``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from test_cli import CliTestCase

from import_support import ALL_SCOPES, FIXTURES, make_client, replay_transport, write_credentials
from fitbit_monitor.api.auth import CredentialStore
from fitbit_monitor.api.catalog import (
    ACTIVITY_SCOPE,
    SCOPE_PREFIX,
    CATALOG,
    HEALTH_METRICS_SCOPE,
    PROFILE_SCOPE,
    SLEEP_SCOPE,
    short_scope,
)
from fitbit_monitor.api.http import fixture_key
from fitbit_monitor.api.client import FitbitClient
from fitbit_monitor.api.errors import NotAuthenticatedError
from fitbit_monitor.api.probe import (
    DATA,
    EMPTY,
    FORBIDDEN,
    HISTORY_BOUNDED,
    HISTORY_BUDGET,
    HISTORY_FLOOR,
    HISTORY_NOT_SEARCHED,
    NO_BUDGET,
    NO_DATA,
    NO_SCOPE,
    RATE_LIMITED,
    SERVER_ERROR,
    UNREACHABLE,
    UNKNOWN,
    classify_status,
    run_probe,
)
from fitbit_monitor.config import ApiSettings
from fitbit_monitor.errors import UsageError

#: Inside the recorded window, so "today" is a day the fixtures answer for.
NOW = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
SINCE = date(2026, 5, 1)
UNTIL = date(2026, 5, 14)

#: The days the history walk asks about for the 2026-05-01..2026-05-14 window.
SAMPLE_DAYS = ["2026-05-14", "2026-05-13", "2026-05-12", "2026-05-10", "2026-05-06", "2026-05-01"]


#: The shape of a Google (AIP-193) error body, with the two fields a report quotes.
AIP_ERROR = (
    '{{"error": {{"code": 403, "status": "{status}", "message": "{message}", "details": ['
    '{{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "{status}"}}]}}}}'
)


def _record(request, *, status: int = 200, body: object = None, headers: dict | None = None) -> dict:
    return {
        "request": {
            "method": request.method if request is not None else "GET",
            "path": request.path() if request is not None else "",
            "query": dict(request.query) if request is not None else {},
            "body": request.json_body() if request is not None else None,
        },
        "response": {"status": status, "headers": headers or {}, "body": {} if body is None else body},
    }


def answer(dataset: str, day: object, **kwargs) -> dict[str, dict]:
    """A record answering exactly what ``dataset`` asks for on one day.

    Built from the catalog rather than written out, because the request that identifies a
    v4 period is a filter clause or a JSON range, not a date in a path - and a test that
    hand-copied those would silently stop matching the moment the catalog changed.
    """
    day = day if isinstance(day, date) else date.fromisoformat(str(day))
    request = CATALOG[dataset].requests_in_chunk(day, day)[0]
    return {fixture_key(request.method, request.path(), dict(request.query), request.json_body()): _record(request, **kwargs)}


def endpoint(dataset: str, **kwargs) -> dict[str, dict]:
    """A record answering *every* request ``dataset`` makes, whatever window it names.

    Keyed by ``METHOD /path`` with no query, which ``replay_transport`` treats as an
    endpoint override: "the SpO2 data type answers 403" is a statement about the data
    type, not about one date.
    """
    request = CATALOG[dataset].requests_in_chunk(SINCE, SINCE)[0]
    return {f"{request.method} {request.path()}": _record(request, **kwargs)}


def profile_record(**kwargs) -> dict[str, dict]:
    return {"GET /users/me/profile": _record(None, **kwargs)}


def sample(dataset: str, day: object = UNTIL) -> dict:
    """A "there is data here" body, in the shape that data type actually answers with.

    The day has to be stamped into the body: the probe reads the dates out of the answer
    rather than assuming the window it asked for came back.
    """
    day = day if isinstance(day, date) else date.fromisoformat(str(day))
    if dataset == "heart_rate":
        return {
            "rollupDataPoints": [
                {
                    "startTime": f"{day.isoformat()}T00:00:00Z",
                    "endTime": f"{day.isoformat()}T00:05:00Z",
                    "heartRate": {"beatsPerMinuteAvg": 61.0, "beatsPerMinuteMin": 55.0, "beatsPerMinuteMax": 72.0},
                }
            ]
        }
    if dataset == "spo2":
        return {
            "dataPoints": [
                {
                    "dailyOxygenSaturation": {
                        "date": {"year": day.year, "month": day.month, "day": day.day},
                        "averagePercentage": 94.3,
                    }
                }
            ]
        }
    raise AssertionError(f"no sample body defined for {dataset}")


class BrokenTransport:
    """A transport that cannot reach the API at all."""

    def __init__(self) -> None:
        from fitbit_monitor.api.errors import APIUnavailableError

        self.error = APIUnavailableError("cannot reach api.fitbit.com: name or service not known")
        self.calls = 0

    def request(self, method, url, headers=None, body=None):
        self.calls += 1
        raise self.error


class ProbeTestCase(unittest.TestCase):
    """A replay-backed client whose clock sits inside the recorded window."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def client(
        self,
        *,
        extra: dict | None = None,
        drop: tuple[str, ...] = (),
        scopes: tuple[str, ...] = ALL_SCOPES,
        api: ApiSettings | None = None,
        transport=None,
    ):
        write_credentials(self.dir / "credentials.json", scopes=scopes)
        return make_client(
            self.dir,
            scopes=scopes,
            api=api,
            sleep=lambda _seconds: None,
            transport=transport or replay_transport(extra or {}, drop=drop),
        )

    def probe(self, client=None, **kwargs):
        kwargs.setdefault("since", SINCE)
        kwargs.setdefault("until", UNTIL)
        kwargs.setdefault("clock", lambda: NOW)
        return run_probe(client or self.client(), **kwargs)

    def dataset(self, report, name: str) -> dict:
        return next(item.as_dict() for item in report.datasets if item.dataset == name)


class GrantedScopesTest(ProbeTestCase):
    def test_everything_granted_leaves_no_gap_and_costs_no_extra_requests(self) -> None:
        report = self.probe()
        self.assertEqual(report.missing_scopes, ())
        self.assertEqual([item.dataset for item in report.usable], [item.dataset for item in report.datasets])
        self.assertEqual(report.exit_code, 0)

    def test_a_missing_scope_names_the_datasets_it_disables_and_asks_neither(self) -> None:
        """Everything except the activity scope: one gap, four datasets it disables.

        The four v4 scopes do not map one-to-one onto datasets the way the legacy six
        did - ``health_metrics_and_measurements`` alone covers heart rate, HRV and SpO2 -
        so a gap is wider now, which is exactly why it has to be named.
        """
        client = self.client(scopes=(HEALTH_METRICS_SCOPE, SLEEP_SCOPE, PROFILE_SCOPE))
        report = self.probe(client)
        gaps = {short_scope(gap.scope): gap for gap in report.missing_scopes}
        self.assertEqual(set(gaps), {"activity_and_fitness.readonly"})
        self.assertEqual(gaps["activity_and_fitness.readonly"].datasets, ("daily_summary",))
        item = self.dataset(report, "daily_summary")
        self.assertEqual(item["status"], NO_SCOPE)
        self.assertEqual(item["requests"], 0, "a dataset the token cannot read is not asked")
        self.assertIn("activity_and_fitness.readonly", item["message"])
        self.assertEqual(report.exit_code, 5, "something is unavailable, so a script should notice")

    def test_a_scope_the_config_never_asked_for_is_named_as_never_requested(self) -> None:
        """A token that was only ever offered one scope is a config problem, not a consent one.

        The distinction matters because the fixes are different: re-consent for a scope
        that was asked for and refused, `[api] scopes` for one never requested at all.
        """
        api = ApiSettings(scopes=(HEALTH_METRICS_SCOPE,))
        client = self.client(scopes=(HEALTH_METRICS_SCOPE,), api=api)
        report = self.probe(client)
        gaps = {short_scope(gap.scope): gap for gap in report.missing_scopes}
        self.assertEqual(
            set(gaps), {"activity_and_fitness.readonly", "sleep.readonly", "profile.readonly"}
        )
        self.assertFalse(gaps["sleep.readonly"].requested)
        self.assertIn("[api] scopes", gaps["sleep.readonly"].line())

    def test_scopes_the_token_has_but_nothing_uses_are_listed_as_unused(self) -> None:
        extra = SCOPE_PREFIX + "settings.readonly"
        client = self.client(scopes=(*ALL_SCOPES, extra))
        report = self.probe(client)
        self.assertEqual(tuple(map(short_scope, report.unused_scopes)), ("settings.readonly",))

    def test_profile_is_wanted_but_no_dataset_needs_it(self) -> None:
        client = self.client(scopes=(HEALTH_METRICS_SCOPE, ACTIVITY_SCOPE, SLEEP_SCOPE))
        report = self.probe(client)
        gap = next(gap for gap in report.missing_scopes if gap.scope == PROFILE_SCOPE)
        self.assertEqual(gap.datasets, ())
        self.assertIn("no dataset in the catalog", gap.line())


class DatasetAvailabilityTest(ProbeTestCase):
    def test_a_dataset_with_data_says_so_with_its_http_status(self) -> None:
        report = self.probe(datasets=["heart_rate"])
        item = self.dataset(report, "heart_rate")
        self.assertEqual(item["status"], DATA)
        self.assertEqual(item["http_status"], 200)
        self.assertTrue(item["usable"])
        self.assertGreaterEqual(item["samples"], 2, "the history walk asked more than one day")

    def test_a_refused_dataset_is_reported_as_forbidden_and_not_walked_backwards(self) -> None:
        extra = endpoint("spo2", status=403, body=AIP_ERROR.format(status="PERMISSION_DENIED", message="no spo2"))
        report = self.probe(self.client(extra=extra))
        item = self.dataset(report, "spo2")
        self.assertEqual(item["status"], FORBIDDEN)
        self.assertEqual(item["http_status"], 403)
        self.assertEqual(item["samples"], 1, "a refusal will not improve further back in time")
        self.assertIn("no spo2", item["message"], "the API's own words are carried through")

    def test_a_dataset_that_answers_404_is_separate_from_a_dataset_that_refuses(self) -> None:
        """v4 answers an empty period with an empty page, so a 404 here is a real 404.

        Which is why it stays its own status: `NOT_FOUND` on a data type means the request
        was wrong (a data type this build invented, a malformed path), while `no_data`
        means the account is fine and the period was quiet.
        """
        extra = endpoint("spo2", status=404, body=AIP_ERROR.format(status="NOT_FOUND", message="no data"))
        report = self.probe(self.client(extra=extra))
        item = self.dataset(report, "spo2")
        self.assertEqual(item["status"], NO_DATA)
        self.assertEqual(item["http_status"], 404)
        self.assertEqual(item["earliest"], None)
        self.assertEqual(report.exit_code, 5, "the other datasets still answered")

    def test_a_server_error_is_retried_by_the_client_and_then_reported(self) -> None:
        extra = endpoint("spo2", status=503, body=AIP_ERROR.format(status="UNAVAILABLE", message="try again"))
        report = self.probe(self.client(extra=extra))
        item = self.dataset(report, "spo2")
        self.assertEqual(item["status"], SERVER_ERROR)
        self.assertEqual(item["http_status"], 503)

    def test_an_unreachable_api_is_a_report_not_an_exception(self) -> None:
        transport = BrokenTransport()
        report = self.probe(self.client(transport=transport))
        self.assertIsNotNone(report.profile_error)
        self.assertIn("cannot reach", report.profile_error)
        for item in report.datasets:
            self.assertEqual(item.status, UNREACHABLE)
        self.assertEqual(report.exit_code, 1, "nothing answered with data")
        self.assertEqual(report.rate_limit["remaining"], None, "no headers were ever seen")
        self.assertIn("no X-RateLimit", "\n".join(report.lines()))

    def test_a_body_that_is_not_json_at_all_is_reported(self) -> None:
        extra = endpoint("heart_rate", body="<html>a proxy ate the request</html>")
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"], history=False)
        self.assertEqual(self.dataset(report, "heart_rate")["status"], EMPTY)

    def test_an_internal_error_body_is_read_as_the_refusal_it_is(self) -> None:
        """A 500 with an AIP body on a rollup: reported, not retried into oblivion."""
        extra = endpoint("heart_rate", status=500, body=AIP_ERROR.format(status="INTERNAL", message="cannot answer"))
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"], history=False)
        self.assertEqual(self.dataset(report, "heart_rate")["status"], SERVER_ERROR)

    def test_the_local_ceiling_is_reported_when_the_api_sends_no_headers(self) -> None:
        """v4 publishes a per-minute quota and no `X-RateLimit-*` headers to read it from.

        A diagnostic that printed only what the API said would print four blanks here, so
        the report says what the client did instead: the requests-per-second it holds
        itself to. Headers are still reported verbatim if a response ever carries them.
        """
        report = self.probe(datasets=["heart_rate"], history=False)
        limit = report.rate_limit
        self.assertIsNone(limit["remaining"])
        self.assertIsNone(limit["limit"])
        self.assertEqual(limit["local_requests_per_second"], 5)
        self.assertEqual(limit["observed_headers"], {})
        line = next(item for item in report.lines() if item.startswith("rate limit"))
        self.assertIn("no ceiling sent by the API", line)
        self.assertIn("5 requests/second", line)

    def test_an_unrecognised_body_is_reported_rather_than_raising(self) -> None:
        extra = endpoint("heart_rate", body={"rollupDataPoints": "this build has never seen this"})
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"], history=False)
        item = self.dataset(report, "heart_rate")
        self.assertEqual(item["status"], UNKNOWN)
        self.assertIn("could not read the response", item["message"])

    def test_a_200_with_nothing_readable_in_it_is_empty_not_data(self) -> None:
        extra = endpoint("heart_rate", body={"rollupDataPoints": []})
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"], history=False)
        item = self.dataset(report, "heart_rate")
        self.assertEqual(item["status"], EMPTY)
        self.assertEqual(item["http_status"], 200)

    def test_a_body_that_is_not_a_json_object_is_reported(self) -> None:
        extra = endpoint("heart_rate", body=["a", "list", "not", "an", "object"])
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"], history=False)
        self.assertEqual(self.dataset(report, "heart_rate")["status"], EMPTY)

    def test_an_http_status_this_build_does_not_know_is_reported_as_unknown(self) -> None:
        extra = endpoint("spo2", status=418, body={})
        report = self.probe(self.client(extra=extra), datasets=["spo2"], history=False)
        item = self.dataset(report, "spo2")
        self.assertEqual(item["status"], UNKNOWN)
        self.assertEqual(item["http_status"], 418)

    def test_classify_names_the_statuses_and_passes_the_rest_as_unknown(self) -> None:
        for status, name in ((401, "unauthorized"), (403, FORBIDDEN), (404, NO_DATA), (410, NO_DATA), (429, "rate_limited")):
            self.assertEqual(classify_status(status), name)
        self.assertEqual(classify_status(500), SERVER_ERROR)
        self.assertEqual(classify_status(418), UNKNOWN)

    def test_a_rate_limited_dataset_is_reported_as_rate_limited(self) -> None:
        extra = endpoint(
            "spo2", status=429, body=AIP_ERROR.format(status="RESOURCE_EXHAUSTED", message="too many"), headers={"Retry-After": "1"}
        )
        report = self.probe(self.client(extra=extra), datasets=["spo2"])
        item = self.dataset(report, "spo2")
        self.assertEqual(item["status"], RATE_LIMITED)
        self.assertIsNone(item["http_status"], "a waited-out rate limit is not reported as a status code")
        self.assertIn("rate limited on", item["message"])
        self.assertEqual(item["samples"], 1, "waiting for a reset is the client's job, not the walk's")

    def test_a_profile_that_said_nothing_is_reported_as_an_unknown_response(self) -> None:
        extra = profile_record(body=["not", "an", "object"])
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"])
        self.assertIn("empty or unrecognisable", report.profile_error)
        self.assertEqual(self.dataset(report, "heart_rate")["status"], DATA, "the dataset is still probed")

    def test_a_profile_that_refuses_does_not_stop_the_datasets(self) -> None:
        extra = profile_record(status=403, body=AIP_ERROR.format(status="PERMISSION_DENIED", message="no profile scope"))
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"])
        self.assertIsNotNone(report.profile_error)
        self.assertEqual(self.dataset(report, "heart_rate")["status"], DATA)
        self.assertIn("profile probe failed", report.notes[0])


class EarliestDataTest(ProbeTestCase):
    def test_the_earliest_date_is_observed_not_assumed(self) -> None:
        report = self.probe(datasets=["heart_rate"])
        item = self.dataset(report, "heart_rate")
        self.assertEqual(item["earliest"], SINCE.isoformat())
        self.assertEqual(item["latest"], UNTIL.isoformat())
        self.assertEqual(item["history"], HISTORY_FLOOR)
        self.assertEqual(item["floor"], "--since 2026-05-01")
        self.assertIn("every sampled day had data", item["message"])

    def test_a_gap_in_the_history_bounds_the_earliest_date(self) -> None:
        # 2026-05-06 and 2026-05-01 are both sampled; saying goodbye to the older ones
        # is what turns "we saw data here" into "the data starts about here".
        extra = answer("heart_rate", "2026-05-06", body={"rollupDataPoints": []})
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"])
        item = self.dataset(report, "heart_rate")
        self.assertEqual(item["history"], HISTORY_BOUNDED)
        self.assertEqual(item["starts_after"], "2026-05-06")
        self.assertEqual(item["earliest"], "2026-05-10")
        self.assertIn("nothing as far back as 2026-05-06", item["message"])

    def test_the_walk_reaches_the_default_floor_of_member_since(self) -> None:
        report = self.probe(datasets=["heart_rate"], since=None)
        self.assertIn("membershipStartDate 2022-11-08", self.dataset(report, "heart_rate")["floor"])

    def test_an_account_with_no_membership_date_falls_back_to_the_default_window(self) -> None:
        """profile.readonly is the one scope a dataset never needs, so it can be missing.

        Without a membership date the walk cannot bound the history, and saying "365d
        default window" is the difference between "we did not look further back" and a
        floor the user mistakes for the account's start.
        """
        extra = profile_record(body={"displayName": "Alex Example"})
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"], since=None)
        self.assertIn("365d default window", self.dataset(report, "heart_rate")["floor"])
        self.assertIsNone(report.account["member_since"])

    def test_a_single_day_window_asks_one_day(self) -> None:
        # 2026-05-04 is not one of the days the recorded walk sampled, so the answer for
        # it is written here instead: one rollup window, one day, one request.
        extra = answer("heart_rate", date(2026, 5, 4), body=sample("heart_rate", date(2026, 5, 4)))
        report = self.probe(self.client(extra=extra), datasets=["heart_rate"], since=date(2026, 5, 4), until=date(2026, 5, 4))
        item = self.dataset(report, "heart_rate")
        self.assertEqual(item["samples"], 1)
        self.assertEqual(item["requests"], 1)
        self.assertEqual(item["earliest"], "2026-05-04")
        self.assertEqual(item["history"], HISTORY_FLOOR)

    def test_no_history_asks_one_day_per_dataset_and_says_it_did_not_bound_anything(self) -> None:
        client = self.client()
        report = self.probe(client, history=False)
        self.assertTrue(all(item.history == HISTORY_NOT_SEARCHED for item in report.datasets))
        self.assertIn("--no-history", report.notes[0])
        self.assertLessEqual(report.requests, 1 + len(report.datasets) * 2 + 1)

    def test_the_request_budget_stops_the_walk_and_says_so(self) -> None:
        report = self.probe(self.client(), max_requests=6)
        self.assertLessEqual(report.requests, 6)
        self.assertIn("budget", json.dumps(report.as_dict()))
        statuses = {item.status for item in report.datasets}
        self.assertTrue({NO_BUDGET, DATA, HISTORY_BUDGET} & statuses or HISTORY_BUDGET in {item.history for item in report.datasets})

    def test_a_budget_of_nothing_asks_nothing_but_the_profile(self) -> None:
        report = self.probe(self.client(), max_requests=0)
        self.assertEqual(report.requests, 1, "only the profile probe spent a request")
        for item in report.datasets:
            self.assertEqual(item.status, NO_BUDGET)

    def test_a_budget_too_small_for_one_day_leaves_the_datasets_unasked(self) -> None:
        # daily_summary costs two requests for a day: with one request left, it is not asked.
        report = self.probe(self.client(), max_requests=2)
        statuses = {item.dataset: item.status for item in report.datasets}
        self.assertEqual(statuses["daily_summary"], NO_BUDGET)
        self.assertLessEqual(report.requests, 2)

    def test_since_after_until_is_clamped_rather_than_probing_nothing(self) -> None:
        report = self.probe(self.client(), datasets=["heart_rate"], since=date(2026, 5, 12), until=date(2026, 5, 10))
        self.assertEqual(report.since, report.until)
        self.assertEqual(self.dataset(report, "heart_rate")["status"], DATA)

    def test_datasets_can_be_named_comma_separated_and_repeated(self) -> None:
        report = self.probe(datasets=["heart_rate,heart_rate", " sleep "])
        self.assertEqual([item.dataset for item in report.datasets], ["heart_rate", "sleep"])


class RateLimitStateTest(ProbeTestCase):
    """The rate-limit section, on a source that sends no rate-limit headers at all.

    v4 gives a per-minute quota in its docs and nothing in the response, so the usual
    answer is "the API said nothing, and here is what this client does about that". The
    header parsing is still exercised, because a Google API that starts sending them
    should be read rather than ignored.
    """

    def test_nothing_is_reported_as_nothing_plus_the_local_ceiling(self) -> None:
        report = self.probe(datasets=["heart_rate"])
        self.assertEqual(report.rate_limit["observed_headers"], {})
        self.assertIsNone(report.rate_limit["remaining"])
        line = next(item for item in report.lines() if item.startswith("rate limit"))
        self.assertIn("no ceiling sent by the API", line)
        self.assertIn("5 requests/second", line)

    def test_headers_are_reported_parsed_and_verbatim_when_the_api_sends_them(self) -> None:
        extra = endpoint(
            "heart_rate",
            body=sample("heart_rate"),
            headers={
                "X-RateLimit-Limit": "150",
                "X-RateLimit-Remaining": "3",
                "X-RateLimit-Limit-Interval": "15m",
                "X-RateLimit-Reset": "847",
            },
        )
        limit = self.probe(self.client(extra=extra), datasets=["heart_rate"], history=False).rate_limit
        self.assertEqual(limit["limit"], 150)
        self.assertEqual(limit["remaining"], 3)
        self.assertEqual(limit["interval"], "15m")
        self.assertEqual(limit["reset_in_seconds"], 847.0)
        self.assertIsNotNone(limit["reset_at"])
        self.assertEqual(limit["observed_headers"]["X-RateLimit-Limit-Interval"], "15m")

    def test_the_human_line_reads_like_a_sentence_when_there_is_a_ceiling(self) -> None:
        extra = endpoint(
            "heart_rate",
            body=sample("heart_rate"),
            headers={"X-RateLimit-Limit": "150", "X-RateLimit-Remaining": "3", "X-RateLimit-Limit-Interval": "15m"},
        )
        lines = self.probe(self.client(extra=extra), datasets=["heart_rate"], history=False).lines()
        line = next(item for item in lines if item.startswith("rate limit"))
        self.assertIn("3 of 150 requests left in the 15m window", line)


class ReportShapeTest(ProbeTestCase):
    def test_the_json_is_a_stable_object_for_scripts(self) -> None:
        payload = self.probe().as_dict()
        for key in (
            "tool",
            "report_version",
            "generated",
            "account",
            "window",
            "scopes",
            "datasets",
            "counts",
            "rate_limit",
            "requests",
            "notes",
            "exit_code",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["tool"], "fm api probe")
        keys = {frozenset(item) for item in payload["datasets"]}
        self.assertEqual(len(keys), 1, "every dataset row carries the same keys")
        self.assertEqual(json.dumps(payload, sort_keys=True).count('"dataset"'), len(payload["datasets"]))

    def test_the_report_carries_the_account_and_no_measurements(self) -> None:
        account = self.probe().as_dict()["account"]
        self.assertEqual(account["health_user_id"], "a1b2c3d4-e5f6-7890-1234-567890abcdef")
        self.assertEqual(account["member_since"], "2022-11-08")
        self.assertEqual(account["time_zone"], "Europe/Berlin")
        # Ids, the membership date and the time zone: enough to explain a window. The
        # profile also carries age, date of birth, height and weight, and none of them
        # belong in a report someone prints in front of other people.
        self.assertEqual(set(account), {"health_user_id", "legacy_user_id", "member_since", "display_name", "time_zone"})
        self.assertNotIn("age", json.dumps(account))
        self.assertNotIn("weight", json.dumps(account))

    def test_every_dataset_name_in_the_catalog_can_be_probed(self) -> None:
        report = self.probe()
        from fitbit_monitor.api.catalog import CATALOG

        self.assertEqual({item.dataset for item in report.datasets}, set(CATALOG))

    def test_the_text_report_names_missing_and_unused_scopes(self) -> None:
        client = self.client(scopes=(HEALTH_METRICS_SCOPE, SLEEP_SCOPE, SCOPE_PREFIX + "settings.readonly"))
        text = "\n".join(self.probe(client).lines())
        self.assertIn("missing activity_and_fitness.readonly - disables: daily_summary", text)
        self.assertIn("missing profile.readonly", text)
        self.assertIn("unused: settings.readonly", text)

    def test_an_unknown_dataset_name_is_a_usage_error_naming_the_valid_ones(self) -> None:
        with self.assertRaises(UsageError) as caught:
            self.probe(datasets=["glucose"])
        self.assertIn("heart_rate", str(caught.exception.hint))

    def test_no_credentials_raises_the_auth_error_rather_than_probing(self) -> None:
        client = FitbitClient(
            ApiSettings(),
            CredentialStore(self.dir / "nothing-here.json"),
            transport=replay_transport(),
            sleep=lambda _seconds: None,
        )
        self.assertFalse(client.authenticated)
        with self.assertRaises(NotAuthenticatedError):
            run_probe(client, clock=lambda: NOW)


class ApiProbeCliTest(CliTestCase):
    """``fm api probe`` run the way a person or a cron line runs it."""

    def setUp(self) -> None:
        super().setUp()
        self.credentials = self.home / "data" / "fitbit-monitor" / "credentials.json"
        self.credentials.parent.mkdir(parents=True, exist_ok=True)

    def probe(self, *extra: str, expect: int | None = None) -> tuple[int, str]:
        return self.fm(
            "api",
            "probe",
            "--fixtures",
            str(FIXTURES),
            "--since",
            SINCE.isoformat(),
            "--until",
            UNTIL.isoformat(),
            *extra,
            expect=expect,
        )

    def test_it_reports_the_three_answers_in_text(self) -> None:
        write_credentials(self.credentials)
        code, text = self.probe(expect=0)
        self.assertIn("scopes    granted:", text)
        self.assertIn("wanted:", text)
        self.assertIn("[data]", text)
        self.assertIn("rate limit", text)
        self.assertIn("requests", text)

    def test_json_is_the_whole_report(self) -> None:
        write_credentials(self.credentials)
        _code, text = self.probe("--json", expect=0)
        payload = json.loads(text)
        self.assertEqual(payload["tool"], "fm api probe")
        self.assertEqual(
            {item["dataset"] for item in payload["datasets"]},
            {"hrv", "spo2", "sleep", "heart_rate", "daily_summary"},
        )
        self.assertEqual(payload["exit_code"], 0)

    def test_a_missing_scope_changes_the_exit_code_and_names_the_dataset(self) -> None:
        write_credentials(self.credentials, scopes=(HEALTH_METRICS_SCOPE, SLEEP_SCOPE, PROFILE_SCOPE))
        code, text = self.probe("--json", expect=5)
        payload = json.loads(text)
        missing = {short_scope(gap["scope"]) for gap in payload["scopes"]["missing"]}
        self.assertEqual(missing, {"activity_and_fitness.readonly"})
        summary = next(item for item in payload["datasets"] if item["dataset"] == "daily_summary")
        self.assertEqual(summary["status"], NO_SCOPE)

    def test_no_credentials_is_reported_in_both_shapes(self) -> None:
        code, text = self.fm("api", "probe", "--fixtures", str(FIXTURES), expect=2)
        self.assertIn("fm auth login", text)
        code, text = self.fm("api", "probe", "--fixtures", str(FIXTURES), "--json", expect=2)
        payload = json.loads(text)
        self.assertFalse(payload["authenticated"])
        self.assertEqual(payload["exit_code"], 2)

    def test_a_credentials_file_the_program_cannot_read_is_reported_not_raised(self) -> None:
        self.credentials.parent.mkdir(parents=True, exist_ok=True)
        self.credentials.write_text('{"written_by_hand": true}', encoding="utf-8")
        os.chmod(self.credentials, 0o600)
        code, text = self.fm("api", "probe", "--fixtures", str(FIXTURES), expect=2)
        self.assertIn("cannot probe", text)
        self.assertIn("fm auth login", text)

    def test_the_window_defaults_to_the_account_and_today(self) -> None:
        write_credentials(self.credentials)
        # No --since/--until: the floor is the account's memberSinceDate and the newest day
        # is today, which the fixture set has nothing for - reported, not fatal.
        code, text = self.fm("api", "probe", "--fixtures", str(FIXTURES), "--no-history", "--json", expect=None)
        payload = json.loads(text)
        self.assertEqual(payload["window"]["since"], "2022-11-08")
        self.assertEqual(code, payload["exit_code"])

    def test_an_unknown_dataset_is_a_usage_error(self) -> None:
        write_credentials(self.credentials)
        code, text = self.probe("--dataset", "glucose")
        self.assertEqual(code, 2)
        self.assertIn("glucose", text)

    def test_until_before_since_is_a_usage_error(self) -> None:
        write_credentials(self.credentials)
        code, text = self.fm(
            "api", "probe", "--fixtures", str(FIXTURES), "--since", "2026-05-10", "--until", "2026-05-01"
        )
        self.assertEqual(code, 2)
        self.assertIn("--until", text)

    def test_no_history_asks_one_window_per_dataset(self) -> None:
        """``--no-history`` is the cheap question: newest day only, one window each.

        The count is not one-per-dataset and never was: a day of ``daily_summary`` is five
        data types, so five requests. v4 makes the *window* cheap, not the day - which is
        why it is 12 requests for five datasets here and was 10 endpoints a day before.
        """
        write_credentials(self.credentials)
        # 2026-05-06 is a day every dataset has a recorded answer for.
        _code, text = self.fm(
            "api",
            "probe",
            "--fixtures",
            str(FIXTURES),
            "--since",
            "2026-05-01",
            "--until",
            "2026-05-06",
            "--no-history",
            "--json",
            expect=0,
        )
        payload = json.loads(text)
        self.assertEqual(payload["counts"].get("data"), len(payload["datasets"]))
        self.assertEqual(payload["requests"], 2 + 1 + 1 + 1 + 1 + 5, "profile, identity, one window each")

    def test_max_requests_bounds_the_probe(self) -> None:
        write_credentials(self.credentials)
        _code, text = self.probe("--max-requests", "4", "--json", expect=None)
        payload = json.loads(text)
        self.assertLessEqual(payload["requests"], 5)

    def test_the_subcommand_is_registered_and_its_flags_reach_the_command(self) -> None:
        from fitbit_monitor.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["api", "probe", "--no-history", "--max-requests", "3", "--json"])
        self.assertTrue(hasattr(args, "func"))
        self.assertFalse(args.history)
        self.assertEqual(args.max_requests, 3)
        self.assertTrue(parser.parse_args(["api", "probe"]).history, "history is on unless --no-history")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
