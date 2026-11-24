"""The retired-source guard: what is refused, where, and what it says instead.

ADR-0002 decided the source and recorded that the legacy Fitbit Web API was switched off
on 2026-11-02. Everything here is about the consequence this build can act on: a host that
is gone must be refused before the request, with a message that does not send the user to
``fm auth login`` for a service that issues no tokens.

No test in this file touches the network - the guard is a pre-flight check, and the live
check is exercised against a fake transport that returns the recorded shapes.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from test_cli import CliTestCase

from import_support import FIXTURES, write_credentials

from fitbit_monitor.api import source
from fitbit_monitor.api.auth import TokenSet
from fitbit_monitor.api.client import FitbitClient
from fitbit_monitor.api.errors import SourceRetiredError
from fitbit_monitor.api.http import RecordingTransport, ReplayTransport, Response, UrllibTransport
from fitbit_monitor.config import ApiSettings
from fitbit_monitor.cli.common import EXIT_OK, EXIT_SOURCE

#: A settings object with the decided (or any live) endpoints, for "would this be allowed".
DECIDED = dict(
    base_url=source.DECIDED_BASE_URL,
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url=source.DECIDED_TOKEN_URL,
)


class FakeTransport:
    """Stands in for a transport, and records what it was asked for."""

    def __init__(self, *, live: bool = True, responses: dict[str, Response] | None = None) -> None:
        self.live = live
        self.requests: list[tuple[str, str]] = []
        self.responses = responses or {}

    def request(self, method, url, headers=None, body=None):
        self.requests.append((method, url))
        for needle, response in self.responses.items():
            if needle in url:
                return response
        return Response(404, b'{"error":{"code":404,"status":"NOT_FOUND","message":"no"}}')


class LegacySettings:
    """The shape of ``[api]`` as shipped: all three endpoints on the retired host."""

    base_url = "https://api.fitbit.com"
    authorize_url = "https://www.fitbit.com/oauth2/authorize"
    token_url = "https://api.fitbit.com/oauth2/token"


class DecidedSettings:
    base_url = source.DECIDED_BASE_URL
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = source.DECIDED_TOKEN_URL


class MixedSettings:
    """The interesting case: a live API with a retired login page left in the config."""

    base_url = "http://127.0.0.1:8000/v4"
    authorize_url = "https://www.fitbit.com/oauth2/authorize"
    token_url = "http://127.0.0.1:8000/token"


class HostClassificationTest(unittest.TestCase):
    def test_every_legacy_host_is_known(self) -> None:
        for url in (
            "https://api.fitbit.com/1/user/-/profile.json",
            "https://www.fitbit.com/oauth2/authorize",
            "https://accounts.fitbit.com/oauth2/authorize",
            "https://dev.fitbit.com/apps/new",
        ):
            self.assertTrue(source.is_retired_host(url), url)

    def test_the_decided_host_and_a_local_standin_are_not_retired(self) -> None:
        for url in (
            source.DECIDED_BASE_URL,
            source.DECIDED_TOKEN_URL,
            "http://127.0.0.1:8000/v4",
            "https://health.googleapis.com.evil.example/v4",
            "",
            "not a url",
        ):
            self.assertFalse(source.is_retired_host(url), url)

    def test_host_of_is_lowercase_and_forgiving(self) -> None:
        self.assertEqual(source.host_of("https://API.Fitbit.com/x"), "api.fitbit.com")
        self.assertEqual(source.host_of(""), "")


class StateTest(unittest.TestCase):
    def test_the_shipped_defaults_are_the_decided_source(self) -> None:
        """The defaults are the ADR's answer, not the retired host.

        This is the assertion that keeps the port from quietly regressing: if a default
        `ApiSettings` ever classifies as retired, every command refuses to dial anything.
        """
        state = source.state_of(ApiSettings())
        self.assertEqual(state.state, source.STATE_DECIDED)
        self.assertFalse(state.is_retired)
        self.assertEqual(state.retired, ())
        self.assertEqual(state.as_dict()["endpoints"]["base_url"], source.DECIDED_BASE_URL)
        self.assertIn("Google Health API", state.short())

    def test_a_config_left_on_the_retired_host_names_all_three_endpoints(self) -> None:
        """Someone who pinned [api] back at api.fitbit.com is refused, with the reasons."""
        state = source.state_of(ApiSettings(base_url="https://api.fitbit.com", authorize_url="https://www.fitbit.com/oauth2/authorize", token_url="https://api.fitbit.com/oauth2/token"))
        self.assertEqual(state.state, source.STATE_RETIRED)
        self.assertTrue(state.is_retired)
        self.assertEqual(
            {name for name, _url in state.retired},
            {"base_url", "authorize_url", "token_url"},
        )
        self.assertIn("api.fitbit.com", state.reason())
        self.assertIn(source.LEGACY_SUNSET, state.reason())
        self.assertIn("retired", state.short())

    def test_the_decided_settings_are_not_retired(self) -> None:
        state = source.state_of(DecidedSettings)
        self.assertEqual(state.state, source.STATE_DECIDED)
        self.assertIn("Google Health API", state.short())

    def test_a_local_standin_is_custom(self) -> None:
        class Local:
            base_url = "http://127.0.0.1:8000/v4"
            authorize_url = "http://127.0.0.1:8000/authorize"
            token_url = "http://127.0.0.1:8000/token"

        self.assertEqual(source.state_of(Local).state, source.STATE_CUSTOM)

    def test_as_dict_is_json_and_names_the_decision(self) -> None:
        payload = json.dumps(source.state_of(LegacySettings).as_dict())
        for needle in (source.LEGACY_SUNSET, source.DECIDED_BASE_URL, source.DECISION_DOC):
            self.assertIn(needle, payload)

    def test_settings_without_urls_do_not_raise(self) -> None:
        class Empty:
            base_url = ""
            authorize_url = ""
            token_url = ""

        self.assertEqual(source.state_of(Empty).state, source.STATE_CUSTOM)


class GuardTest(unittest.TestCase):
    def test_dialling_a_retired_host_is_refused(self) -> None:
        with self.assertRaises(SourceRetiredError) as caught:
            source.guard(LegacySettings)
        error = caught.exception
        self.assertEqual(error.exit_code, EXIT_SOURCE)
        self.assertIn(source.LEGACY_SUNSET, error.message)
        # The advice matters more than the code: it must not be "log in again".
        self.assertNotIn("auth login", f"{error.message} {error.hint}")
        self.assertIn("source csv", error.hint, "the alternative that works is named")
        self.assertIn("fm import --source csv", error.hint)

    def test_an_offline_transport_is_never_refused(self) -> None:
        """The fixtures are the dev and CI source (ADR-0002 section 8); refusing them breaks
        the one import path that still lands data."""
        state = source.guard(LegacySettings, live=False)
        self.assertEqual(state.state, source.STATE_RETIRED, "reported, not refused")

    def test_a_live_host_passes_and_extra_urls_are_checked(self) -> None:
        self.assertEqual(source.guard(DecidedSettings).state, source.STATE_DECIDED)
        with self.assertRaises(SourceRetiredError):
            source.guard(DecidedSettings, extra=("https://api.fitbit.com/oauth2/token",))

    def test_what_counts_depends_on_what_is_being_dialled(self) -> None:
        """A live API with a retired consent page left in the config is not a blocker for a
        command that already has a token - and is not one for a login either, because the
        browser dials that URL, not the program."""
        source.guard(MixedSettings, settings=source.DATA_PLANE_SETTINGS)
        source.guard(MixedSettings, settings=source.TOKEN_PLANE_SETTINGS)
        with self.assertRaises(SourceRetiredError):
            source.guard(MixedSettings, settings=source.ALL_ENDPOINT_SETTINGS)


class TransportLivenessTest(unittest.TestCase):
    def test_only_the_real_transport_is_live(self) -> None:
        self.assertTrue(UrllibTransport.live)
        self.assertFalse(ReplayTransport.live)
        self.assertTrue(RecordingTransport(FakeTransport(live=True), Path("/tmp/nope-unused")).live)
        self.assertFalse(RecordingTransport(FakeTransport(live=False), Path("/tmp/nope-unused")).live)

    def test_a_transport_that_does_not_answer_is_not_live(self) -> None:
        class Silent:
            def request(self, method, url, headers=None, body=None):  # pragma: no cover
                raise AssertionError("not called")

        self.assertFalse(bool(getattr(Silent(), "live", False)))


class ClientGuardTest(unittest.TestCase):
    def client(self, transport, *, settings=None, tokens: bool = True) -> FitbitClient:
        client = FitbitClient(
            settings or ApiSettings(),
            credentials=_NullCredentials(),
            transport=transport,
        )
        if tokens:
            client.set_tokens(
                TokenSet(
                    access_token="a",
                    refresh_token="b",
                    expires_at=datetime.now(UTC) + timedelta(hours=8),
                    scopes=("profile",),
                )
            )
        return client

    def test_get_refuses_a_retired_base_url_without_dialling_it(self) -> None:
        transport = FakeTransport(live=True)
        client = self.client(transport, settings=ApiSettings(base_url="https://api.fitbit.com"))
        self.assertTrue(client.live)
        with self.assertRaises(SourceRetiredError):
            client.get("/users/me/profile")
        self.assertEqual(transport.requests, [], "the request was never spent")

    def test_a_retired_token_url_refuses_post_form_and_refresh(self) -> None:
        api = ApiSettings(
            base_url="http://127.0.0.1:8000/v4",  # a live API...
            token_url="https://api.fitbit.com/oauth2/token",  # ...with a retired login
        )
        transport = FakeTransport(live=True)
        client = self.client(transport, settings=api)
        with self.assertRaises(SourceRetiredError):
            client.post_form("https://api.fitbit.com/oauth2/token", {"grant_type": "refresh_token"})
        with self.assertRaises(SourceRetiredError):
            client.ensure_fresh(force=True)
        self.assertEqual(transport.requests, [])

    def test_replay_transport_is_free_to_use_the_retired_urls(self) -> None:
        """The whole fixture suite depends on this.

        Even with a config that names the dead host: the guard binds transports that can
        reach the internet, because ADR-0002 §8 makes the replay corpus the dev and CI
        source and a fixture is not a dial.
        """
        replay = ReplayTransport(FIXTURES)
        client = self.client(replay, settings=ApiSettings(base_url="https://api.fitbit.com"))
        self.assertFalse(client.live)
        response = client.get("/users/me/profile")
        self.assertTrue(response.ok)

    def test_a_live_decided_client_is_not_refused(self) -> None:
        api = ApiSettings(**DECIDED)
        transport = FakeTransport(live=True, responses={"profile": Response(200, b'{"displayName": "me"}')})
        client = self.client(transport, settings=api)
        client.get("/users/me/profile")
        self.assertEqual(len(transport.requests), 1)

    def test_source_state_does_not_raise_and_reports_the_config(self) -> None:
        self.assertEqual(self.client(FakeTransport()).source_state().state, source.STATE_DECIDED)
        retired = self.client(FakeTransport(), settings=ApiSettings(base_url="https://api.fitbit.com"))
        self.assertEqual(retired.source_state().state, source.STATE_RETIRED)


class LiveCheckTest(unittest.TestCase):
    """`fm api source --verify` against the bodies the real hosts return."""

    RESPONSES = {
        "api.fitbit.com/1/user": Response(
            401,
            b'{"error":{"code":401,"message":"...","status":"UNAUTHENTICATED"}}',
        ),
        "accounts.fitbit.com": Response(200, b"<html>Fitbit authorize</html>"),
        "health.googleapis.com": Response(
            401,
            b'{"error":{"code":401,"status":"UNAUTHENTICATED","details":['
            b'{"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":"CREDENTIALS_MISSING",'
            b'"metadata":{"service":"health.googleapis.com"}}]}}',
        ),
    }

    def observations(self, transport=None):
        return source.check_alive(transport or FakeTransport(responses=self.RESPONSES))

    def test_the_measured_answers_are_classified_and_match_the_adr(self) -> None:
        observations = {o.label: o for o in self.observations()}
        self.assertEqual(observations["legacy api"].status, 401)
        self.assertEqual(observations["legacy api"].verdict, source.ALIVE)
        self.assertIn("UNAUTHENTICATED", observations["legacy api"].signal)
        self.assertEqual(observations["legacy oauth"].verdict, source.RENDERS)
        self.assertIn("CREDENTIALS_MISSING", observations["decided api"].signal)
        for observation in observations.values():
            self.assertTrue(observation.agrees_with_adr, observation.line())
            self.assertIn(observation.verdict, observation.line())

    def test_a_410_is_gone_and_an_answer_that_differs_is_flagged_as_drift(self) -> None:
        transport = FakeTransport(
            responses={
                "api.fitbit.com/1/user": Response(410, b""),
                "accounts.fitbit.com": Response(200, b"<html/>"),
                "health.googleapis.com": Response(200, b'{"dataPoints":[]}'),
            }
        )
        observations = {o.label: o for o in self.observations(transport)}
        self.assertEqual(observations["legacy api"].verdict, source.GONE)
        self.assertFalse(
            observations["legacy api"].agrees_with_adr,
            "the ADR measured a 401, so a 410 is new information about the host either way",
        )
        self.assertEqual(observations["decided api"].verdict, source.UNEXPECTEDLY_OPEN)
        self.assertFalse(observations["decided api"].agrees_with_adr)

    def test_an_unreachable_host_is_a_verdict_not_a_crash(self) -> None:
        from fitbit_monitor.api.errors import APIUnavailableError

        class Down:
            live = True

            def request(self, method, url, headers=None, body=None):
                raise APIUnavailableError(f"cannot reach {url}")

        observations = self.observations(Down())
        self.assertEqual(len(observations), 3)
        for observation in observations:
            self.assertEqual(observation.verdict, source.UNREACHABLE)
            self.assertIsNone(observation.status)
            self.assertFalse(observation.agrees_with_adr)

    def test_the_signal_is_a_code_and_never_a_dump_of_the_body(self) -> None:
        leaked = Response(200, json.dumps({"heart": [72, 73], "note": "keep this out"}).encode())
        self.assertEqual(source._signal(leaked), "")

    def test_the_legacy_fitbit_error_dialect_is_read_too(self) -> None:
        body = b'{"errors":[{"errorType":"invalid_token","message":"Access token invalid"}]}'
        self.assertEqual(source._signal(Response(401, body)), "invalid_token")


class _NullCredentials:
    """A credential store that holds nothing, so the client does not read the real file."""

    exists = False

    def load_or_none(self):
        return None

    def load(self):
        from fitbit_monitor.api.errors import NotAuthenticatedError

        raise NotAuthenticatedError("none")

    def save(self, tokens):  # pragma: no cover
        raise AssertionError("nothing should be saved")


#: An ``[api]`` table left pointing at the retired source: what a config written before
#: the port looks like, and the only way a current build gets into this state.
RETIRED_API = (
    "[api]\n"
    'base_url = "https://api.fitbit.com"\n'
    'authorize_url = "https://www.fitbit.com/oauth2/authorize"\n'
    'token_url = "https://api.fitbit.com/oauth2/token"\n'
)


class RetiredSourceCliTest(CliTestCase):
    """The commands a user reaches when an import lands nothing.

    Every test here starts from a config pinned at the retired host, because that - not
    the shipped default - is what produces the refusal. The guard exists for the config
    that outlived the service, and for the person who copy-pasted the old host back in
    when their token stopped working.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write_config(RETIRED_API)

    def config_path(self) -> Path:
        path = self.home / "config" / "fitbit-monitor" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def credentials_path(self) -> Path:
        return self.home / "data" / "fitbit-monitor" / "credentials.json"

    def write_config(self, text: str) -> None:
        self.config_path().write_text(text, encoding="utf-8")

    def test_import_refuses_the_retired_source_and_says_what_works(self) -> None:
        write_credentials(self.credentials_path)
        code, text = self.fm("import", "--since", "2026-05-01", "--until", "2026-05-02", expect=EXIT_SOURCE)
        self.assertIn("retired", text)
        self.assertIn(source.LEGACY_SUNSET, text)
        self.assertIn("fm import --source csv", text)
        self.assertNotIn("run `fm auth login` again", text, "the dead host must not read as a bad login")
        self.assertNotIn("Traceback", text)

    def test_the_refusal_precedes_the_credentials_check(self) -> None:
        """No token *and* a dead host: the useful answer is the dead host."""
        code, text = self.fm("import", "--dry-run", expect=EXIT_SOURCE)
        self.assertIn("retired", text)

    def test_api_probe_refuses_without_spending_requests(self) -> None:
        write_credentials(self.credentials_path)
        code, text = self.fm("api", "probe", expect=EXIT_SOURCE)
        self.assertIn("cannot probe", text)
        self.assertIn("retired", text)
        code, text = self.fm("api", "probe", "--json", expect=EXIT_SOURCE)
        payload = json.loads(text)
        self.assertEqual(payload["exit_code"], EXIT_SOURCE)
        self.assertIn("retired", payload["error"])
        self.assertIn("source csv", payload["hint"])

    def test_login_refuses_before_the_browser_opens(self) -> None:
        self.config_path().write_text(RETIRED_API + 'client_id = "test-client"\nclient_secret = "test-secret"\n')
        write_credentials(self.credentials_path)
        code, text = self.fm("auth", "login", "--manual", "the-code", expect=EXIT_SOURCE)
        self.assertNotIn("open this URL", text, "the user is not sent to consent against a dead endpoint")
        self.assertIn("retired", text)

    def test_login_still_runs_against_a_live_token_endpoint(self) -> None:
        """The guard refuses the dead host, not the login command."""
        self.write_config(
            '[api]\nclient_id = "test-client"\nbase_url = "http://127.0.0.1:8000/v4"\n'
            'token_url = "http://127.0.0.1:8000/token"\n'
        )
        with mock.patch("fitbit_monitor.cli.auth_cmd.exchange_code") as exchange:
            exchange.return_value = TokenSet(
                access_token="a",
                refresh_token="b",
                expires_at=datetime.now(UTC) + timedelta(hours=8),
                scopes=("profile",),
                user_id="7",
            )
            code, text = self.fm("auth", "login", "--manual", "the-code", expect=EXIT_OK)
        self.assertIn("stored tokens", text.lower())

    def test_auth_status_notes_the_retirement_without_hiding_the_token_state(self) -> None:
        write_credentials(self.credentials_path)
        code, text = self.fm("auth", "status", expect=EXIT_OK)
        self.assertIn("authenticated as", text)
        self.assertIn("retired", text)
        code, payload = self.fm("auth", "status", "--json", expect=EXIT_OK)
        report = json.loads(payload)
        self.assertTrue(report["authenticated"])
        self.assertIn("retired", report["warning"])

    def test_status_without_credentials_keeps_the_login_hint_and_adds_the_note(self) -> None:
        _code, text = self.fm("auth", "status", expect=EXIT_OK)
        self.assertIn("fm auth login", text, "the command name is still the next thing to try, with the caveat")
        self.assertIn("retired", text)

    def test_config_show_names_the_source_state(self) -> None:
        _code, text = self.fm("config", "show", expect=EXIT_OK)
        self.assertIn("source:", text)
        self.assertIn("retired", text)
        _code, payload = self.fm("config", "show", "--json", expect=EXIT_OK)
        self.assertEqual(json.loads(payload)["source"]["state"], source.STATE_RETIRED)

    def test_config_init_tells_the_truth_about_the_api_path(self) -> None:
        _code, text = self.fm("config", "init", "--print", expect=EXIT_OK)
        self.assertIn(source.LEGACY_SUNSET, text)
        self.assertIn("fm import --source csv", text)
        self.assertNotIn("dev.fitbit.com/apps", text, "the sunset registration page is not an instruction")

    def test_doctor_reports_the_source_without_failing_the_run(self) -> None:
        """A warning, not a failure: a check that shouts at a config mistake the tool can
        still work around is a check that gets ignored when something is genuinely wrong."""
        write_credentials(self.credentials_path)
        _code, text = self.fm("doctor", "--json", expect=EXIT_OK)
        checks = {check["check"]: check for check in json.loads(text)["checks"]}
        source_check = checks["source"]
        self.assertEqual(source_check["status"], "warn")
        self.assertEqual(source_check["state"], source.STATE_RETIRED)
        self.assertTrue(source_check["tokens"])

    def test_doctor_with_a_live_source_is_a_plain_ok(self) -> None:
        self.write_config(
            f'[api]\nbase_url = "{source.DECIDED_BASE_URL}"\n'
            f'authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"\n'
            f'token_url = "{source.DECIDED_TOKEN_URL}"\n'
        )
        _code, payload = self.fm("doctor", "--json", expect=EXIT_OK)
        checks = {check["check"]: check for check in json.loads(payload)["checks"]}
        self.assertEqual(checks["source"]["status"], "ok")

    def test_api_source_reports_the_configuration(self) -> None:
        code, text = self.fm("api", "source", expect=EXIT_SOURCE)
        self.assertIn("retired", text)
        self.assertIn("base_url", text)
        self.assertIn("fm import --source csv", text)

    def test_api_source_json_is_the_same_facts_as_data(self) -> None:
        _code, payload = self.fm("api", "source", "--json", expect=EXIT_SOURCE)
        report = json.loads(payload)
        self.assertEqual(report["state"], source.STATE_RETIRED)
        self.assertEqual(report["exit_code"], EXIT_SOURCE)
        self.assertEqual(report["sunset"], source.LEGACY_SUNSET)
        self.assertIn("base_url", report["retired_endpoints"])
        self.assertEqual(report["live"], [], "not verified")

    def test_api_source_verify_asks_the_hosts_and_labels_the_answers(self) -> None:
        transport = FakeTransport(live=True, responses=LiveCheckTest.RESPONSES)
        from fitbit_monitor.cli import api_cmd

        retired = ApiSettings(
            base_url="https://api.fitbit.com",
            authorize_url="https://www.fitbit.com/oauth2/authorize",
            token_url="https://api.fitbit.com/oauth2/token",
        )

        def fake_api_client(self, *args, **kwargs):  # noqa: ANN001 - patching a method
            return FitbitClient(retired, credentials=_NullCredentials(), transport=transport)

        with mock.patch.object(api_cmd.Session, "api_client", fake_api_client):
            code, text = self.fm("api", "source", "--verify", expect=EXIT_SOURCE)
        self.assertIn("live check", text)
        self.assertIn("UNAUTHENTICATED", text)
        self.assertIn("CREDENTIALS_MISSING", text)
        self.assertEqual(len(transport.requests), 3, "one request per target, no more")

    def test_api_source_verify_on_an_offline_transport_says_so(self) -> None:
        self._setenv("FM_FIXTURES", str(FIXTURES))
        code, text = self.fm("api", "source", "--verify", "--json", expect=EXIT_SOURCE)
        report = json.loads(text)
        self.assertEqual(report["live"], [])
        self.assertIn("offline", report["note"])

    def test_api_source_exits_zero_for_a_live_source(self) -> None:
        self.write_config(
            f'[api]\nbase_url = "{source.DECIDED_BASE_URL}"\n'
            f'authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"\n'
            f'token_url = "{source.DECIDED_TOKEN_URL}"\n'
        )
        code, text = self.fm("api", "source", expect=EXIT_OK)
        self.assertIn("Google Health API", text)

    def test_the_help_text_documents_the_new_command(self) -> None:
        _code, text = self.fm("api", "--help", expect=EXIT_OK)
        self.assertIn("source", text)
        _code, text = self.fm("api", "source", "--help", expect=EXIT_OK)
        self.assertIn("--verify", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
