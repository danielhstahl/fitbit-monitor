"""Tests for the HTTP layer: the response type, the transports, and the client's manners.

Everything here runs offline. The live transport is exercised against a
``ThreadingHTTPServer`` bound to loopback - real HTTP, no internet, no secrets - and
everything else runs against scripted or recorded transports, which is also how the
import tests work.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fitbit_monitor.api.auth import (
    REDIRECT_PATH,
    CredentialStore,
    LoopbackCallback,
    TokenSet,
    exchange_code,
    pkce_challenge,
    refresh,
)
from fitbit_monitor.api.catalog import HEALTH_METRICS_SCOPE, PROFILE_SCOPE
from fitbit_monitor.api.client import FitbitClient, RateLimit
from fitbit_monitor.api.errors import (
    APIUnavailableError,
    CredentialsExpiredError,
    HTTPError,
    NotAuthenticatedError,
    RateLimitError,
    ReplayMissError,
)
from fitbit_monitor.api.http import (
    RecordingTransport,
    ReplayTransport,
    Response,
    UrllibTransport,
    fixture_filename,
    fixture_key,
    load_fixtures,
)
from fitbit_monitor.config import ApiSettings, ImportSettings
from fitbit_monitor.errors import UsageError

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "api"


class ScriptedTransport:
    """A transport that replays a list, and records what it was asked for."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, headers=None, body=None) -> Response:
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {}), "body": body or b""})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tokens(**overrides) -> TokenSet:
    base = dict(
        access_token="access-1",
        refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(hours=6),
        scopes=(HEALTH_METRICS_SCOPE, PROFILE_SCOPE),
        user_id="42",
    )
    base.update(overrides)
    return TokenSet(**base)


class ResponseTest(unittest.TestCase):
    def test_headers_are_case_insensitive_and_json_is_forgiving(self) -> None:
        response = Response(200, b'{"a": 1}', {"X-RateLimit-Remaining": "7"})
        self.assertEqual(response.header("x-ratelimit-remaining"), "7")
        self.assertIsNone(response.header("nope"))
        self.assertEqual(response.json(), {"a": 1})
        self.assertEqual(Response(204, b"   ").json(), {})
        self.assertTrue(response.ok)
        self.assertFalse(Response(404, b"").ok)
        self.assertTrue(Response(304, b"").not_modified)

    def test_non_json_body_is_an_api_error(self) -> None:
        with self.assertRaises(APIUnavailableError):
            Response(200, b"<html>nope</html>").json()


class FixtureFormatTest(unittest.TestCase):
    def test_key_is_stable_and_query_aware(self) -> None:
        self.assertEqual(fixture_key("get", "/a/b"), "GET /a/b")
        self.assertEqual(fixture_key("GET", "a/b", {"b": "2", "a": "1"}), "GET /a/b?a=1&b=2")
        self.assertEqual(fixture_key("GET", "/a/b", {"a": "1"}), fixture_key("GET", "/a/b", {"a": "1"}))
        self.assertEqual(fixture_filename(fixture_key("GET", "/a/b?x=1")), "GET_a_b_x_1.json")

    def test_recorded_fixtures_load_and_replay(self) -> None:
        transport = ReplayTransport(FIXTURES)
        self.assertGreater(len(transport), 60)
        response = transport.request("GET", "https://health.googleapis.com/v4/users/me/profile")
        self.assertEqual(response.status, 200)
        self.assertIn("membershipStartDate", response.json())
        self.assertEqual(transport.misses, 0)

    def test_the_health_user_id_is_normalised(self) -> None:
        """A fixture set is portable between accounts, so the user id is not part of a key.

        ``users/me`` is what the catalog asks for and what the corpus records, but
        ``[api] user_id`` may name the numeric ``healthUserId`` instead - and a corpus
        recorded on one account has to replay on the other.
        """
        recorded = json.loads(sorted(FIXTURES.glob("*.json"))[0].read_text())
        request = recorded["request"]
        path = request["path"]
        self.assertIn("/users/me/", path)
        query = "&".join(f"{k}={v}" for k, v in sorted(request["query"].items()))
        transport = ReplayTransport(FIXTURES)
        asked = path.replace("/users/me/", "/users/9876543/") + (f"?{query}" if query else "")
        response = transport.request(request["method"], f"https://health.googleapis.com/v4{asked}")
        self.assertEqual(response.status, 200, asked)
        self.assertEqual(transport.misses, 0)

    def test_unrecorded_request_is_a_named_miss(self) -> None:
        transport = ReplayTransport(FIXTURES)
        with self.assertRaises(ReplayMissError) as caught:
            transport.request("GET", "https://health.googleapis.com/v4/users/me/nope/nothere")
        self.assertIn("/users/me/nope/nothere", str(caught.exception))
        self.assertEqual(transport.misses, 1)

    def test_hand_written_fixture_can_name_its_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.json"
            path.write_text(json.dumps({"key": "GET /x", "response": {"status": 201, "body": {"ok": True}}}))
            self.assertEqual(load_fixtures(Path(tmp))["GET /x"]["response"]["status"], 201)

    def test_bad_fixture_json_is_reported_with_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "broken.json").write_text("{not json")
            with self.assertRaises(ReplayMissError):
                load_fixtures(Path(tmp))

    def test_recording_then_replaying_round_trips(self) -> None:
        inner = ScriptedTransport(
            [Response(200, b'{"heart": []}', {"X-RateLimit-Remaining": "9"}), Response(404, b'{"errors": []}')]
        )
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RecordingTransport(inner, Path(tmp))
            recorder.request("GET", "https://health.googleapis.com/v4/users/me/dataTypes/heart-rate/dataPoints")
            recorder.request("GET", "https://health.googleapis.com/v4/users/me/dataTypes/sleep/dataPoints")
            replay = ReplayTransport(Path(tmp))
            self.assertEqual(len(replay), 2)
            self.assertEqual(replay.request("GET", "https://health.googleapis.com/v4/users/me/dataTypes/heart-rate/dataPoints").json(), {"heart": []})
            self.assertEqual(replay.request("GET", "https://health.googleapis.com/v4/users/me/dataTypes/sleep/dataPoints").status, 404)
            self.assertEqual(replay.misses, 0)


class _LocalAPI(BaseHTTPRequestHandler):
    """A loopback stand-in for the v4 endpoint, so the urllib path is really exercised."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/gone"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"errors":[{"message":"nope"}]}')
            return
        body = json.dumps({"path": self.path, "auth": self.headers.get("Authorization")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-RateLimit-Remaining", "12")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


class UrllibTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalAPI)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.port}"

    def test_a_real_request_round_trips(self) -> None:
        transport = UrllibTransport(timeout=5)
        response = transport.request("GET", f"{self.base}/hello", {"Authorization": "Bearer x"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("X-RateLimit-Remaining"), "12")
        self.assertEqual(response.json()["auth"], "Bearer x")

    def test_an_error_status_is_a_response_not_an_exception(self) -> None:
        self.assertEqual(UrllibTransport(timeout=5).request("GET", f"{self.base}/gone").status, 404)

    def test_connection_refused_is_an_api_unavailability(self) -> None:
        with self.assertRaises(APIUnavailableError):
            UrllibTransport(timeout=2).request("GET", "http://127.0.0.1:1/nothing")


class ClientBehaviourTest(unittest.TestCase):
    def client(self, responses, *, settings=None, api=None, etag_path=None, sleeps=None):
        tmp = tempfile.mkdtemp()
        credentials = CredentialStore(Path(tmp) / "credentials.json")
        credentials.save(tokens())
        transport = ScriptedTransport(responses)
        client = FitbitClient(
            api or ApiSettings(),
            credentials,
            import_settings=settings or ImportSettings(rate_limit_reserve=5, max_wait_seconds=30),
            transport=transport,
            sleep=(sleeps.append if sleeps is not None else lambda _s: None),
            etag_cache_path=etag_path,
        )
        return client, transport

    def test_bearer_token_and_headers_are_sent(self) -> None:
        client, transport = self.client([Response(200, b'{"ok": true}', {"X-RateLimit-Remaining": "140"})])
        self.assertEqual(client.get_json("/users/me/profile"), {"ok": True})
        first = transport.requests[0]
        self.assertEqual((first["method"], first["url"]), ("GET", "https://health.googleapis.com/v4/users/me/profile"))
        self.assertEqual(first["headers"]["Authorization"], "Bearer access-1")
        self.assertEqual(first["headers"]["Accept"], "application/json")
        self.assertEqual(client.rate_limit.remaining, 140)
        self.assertEqual(client.requests, 1)

    def test_no_credentials_is_a_typed_error(self) -> None:
        client, _ = self.client([])
        client._tokens = None
        client.credentials.delete()
        with self.assertRaises(NotAuthenticatedError):
            client.get("/users/me/profile")

    def test_a_configured_health_user_id_replaces_me(self) -> None:
        client, transport = self.client([Response(200, b"{}")], api=ApiSettings(user_id="9999"))
        client.get("/users/me/dataTypes/sleep/dataPoints")
        self.assertIn("/users/9999/dataTypes/sleep/", transport.requests[0]["url"])

    def test_a_transient_status_is_retried_with_backoff(self) -> None:
        """503 then 504 then data: the documented trio, retried with jittered backoff."""
        sleeps: list[float] = []
        client, transport = self.client(
            [Response(503, b"boom"), Response(504, b"boom"), Response(200, b'{"ok":1}')], sleeps=sleeps
        )
        self.assertEqual(client.get_json("/users/me/dataTypes/steps/dataPoints"), {"ok": 1})
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(len(sleeps), 2, "exponential backoff between attempts")
        self.assertTrue(all(seconds > 0 for seconds in sleeps), "the backoff actually waits")

    def test_an_internal_error_is_not_retried(self) -> None:
        """500 INTERNAL is not in the trio the API documents for retry.

        AIP reserves it for "the service was unable to return something it has for this
        request", which in practice means the query itself - a rollup window the analyzer
        cannot answer, most often. Repeating it four times costs the same quota and gets
        the same answer, so it is reported as the refusal it is.
        """
        client, transport = self.client([Response(500, b'{"error":{"code":500,"status":"INTERNAL","message":"bad"}}')])
        with self.assertRaises(HTTPError) as caught:
            client.get("/users/me/dataTypes/steps/dataPoints")
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(len(transport.requests), 1)

    def test_giving_up_after_the_retry_budget(self) -> None:
        client, transport = self.client(
            [Response(503, b"a"), Response(503, b"b"), Response(503, b"c"), Response(503, b"d")]
        )
        with self.assertRaises(HTTPError) as caught:
            client.get("/users/me/dataTypes/steps/dataPoints")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(transport.requests), 4, "retries plus the first attempt")

    def test_4xx_other_than_auth_is_not_retried(self) -> None:
        client, transport = self.client([Response(400, b'{"errors":[{"message":"bad date","location":"date"}]}')])
        with self.assertRaises(HTTPError) as caught:
            client.get("/users/me/dataTypes/steps/dataPoints")
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("bad date", str(caught.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_429_waits_for_the_reset_and_retries(self) -> None:
        sleeps: list[float] = []
        client, transport = self.client(
            [Response(429, b"", {"Retry-After": "3"}), Response(200, b'{"ok":1}')], sleeps=sleeps
        )
        self.assertEqual(client.get_json("/users/me/dataTypes/steps/dataPoints"), {"ok": 1})
        self.assertEqual(sleeps[0], 3.0)

    def test_429_past_the_patience_limit_raises_rate_limit(self) -> None:
        client, transport = self.client([Response(429, b"", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "900"})])
        with self.assertRaises(RateLimitError):
            client.get("/users/me/dataTypes/steps/dataPoints")

    def test_the_budget_is_respected_before_the_ceiling(self) -> None:
        sleeps: list[float] = []
        client, _ = self.client(
            [Response(200, b'{"ok":1}', {"X-RateLimit-Remaining": "2", "X-RateLimit-Reset": "12"}), Response(200, b'{"ok":2}')],
            sleeps=sleeps,
        )
        client.get_json("/users/me/dataTypes/steps/dataPoints")
        client.get_json("/users/me/dataTypes/distance/dataPoints")
        self.assertEqual(sleeps, [12.0], "stop before the ceiling, wait for the reset")

    def test_rate_limit_too_far_away_stops_the_run(self) -> None:
        client, _ = self.client([Response(200, b"{}", {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "5000"})])
        client.get("/users/me/dataTypes/steps/dataPoints")
        with self.assertRaises(RateLimitError):
            client.get("/users/me/dataTypes/distance/dataPoints")

    def test_an_expired_token_is_refreshed_before_the_request(self) -> None:
        sleeps: list[float] = []
        transport = ScriptedTransport(
            [
                Response(200, b'{"access_token":"access-2","refresh_token":"refresh-2","expires_in":28800,"scope":"profile"}'),
                Response(200, b'{"ok":true}'),
            ]
        )
        tmp = Path(tempfile.mkdtemp())
        credentials = CredentialStore(tmp / "credentials.json")
        credentials.save(tokens(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        client = FitbitClient(
            ApiSettings(client_id="cid", client_secret="csecret"),
            credentials,
            transport=transport,
            sleep=lambda _s: None,
            import_settings=ImportSettings(),
        )
        self.assertEqual(client.get_json("/users/me/profile"), {"ok": True})
        self.assertEqual(credentials.load().access_token, "access-2", "the refreshed token is persisted")
        self.assertEqual(transport.requests[1]["headers"]["Authorization"], "Bearer access-2")
        self.assertTrue((tmp / "credentials.json").exists())
        self.assertEqual(sleeps, [])

    def test_a_401_refreshes_once_then_gives_up(self) -> None:
        transport = ScriptedTransport(
            [
                Response(401, b'{"errors":[{"message":"invalid_token"}]}'),
                Response(200, b'{"access_token":"a2","refresh_token":"r2","expires_in":28800}'),
                Response(401, b'{"errors":[{"message":"invalid_token"}]}'),
            ]
        )
        credentials = CredentialStore(Path(tempfile.mkdtemp()) / "credentials.json")
        credentials.save(tokens())
        client = FitbitClient(
            ApiSettings(client_id="cid", client_secret="c"), credentials, transport=transport, import_settings=ImportSettings()
        )
        with self.assertRaises(HTTPError):
            client.get("/users/me/profile")
        self.assertEqual(len(transport.requests), 3)

    def test_a_refused_refresh_names_the_fix(self) -> None:
        credentials = CredentialStore(Path(tempfile.mkdtemp()) / "credentials.json")
        credentials.save(tokens(expires_at=datetime.now(UTC) - timedelta(minutes=5), refresh_token=""))
        client = FitbitClient(ApiSettings(), credentials, transport=ScriptedTransport([]), import_settings=ImportSettings())
        with self.assertRaises(CredentialsExpiredError) as caught:
            client.ensure_fresh()
        self.assertIn("refresh token", str(caught.exception))

    def test_etags_are_cached_and_304s_reuse_the_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "etag-cache.json"
            transport = ScriptedTransport(
                [
                    Response(200, b'{"heart":[1]}', {"ETag": '"abc"'}),
                    Response(304, b"", {"ETag": '"abc"'}),
                ]
            )
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            credentials.save(tokens())
            client = FitbitClient(
                ApiSettings(), credentials, transport=transport, import_settings=ImportSettings(), etag_cache_path=cache_path
            )
            self.assertEqual(client.get_json("/users/me/dataTypes/heart-rate/dataPoints"), {"heart": [1]})
            self.assertEqual(client.get_json("/users/me/dataTypes/heart-rate/dataPoints"), {"heart": [1]})
            self.assertEqual(transport.requests[1]["headers"]["If-None-Match"], '"abc"')
            self.assertEqual(client.not_modified, 1)
            client.flush()
            self.assertIn("heart", cache_path.read_text())
            # a fresh client loads the cache from disk and gets a 304 again
            transport2 = ScriptedTransport([Response(304, b"", {"ETag": '"abc"'})])
            client2 = FitbitClient(
                ApiSettings(), credentials, transport=transport2, import_settings=ImportSettings(), etag_cache_path=cache_path
            )
            self.assertEqual(client2.get_json("/users/me/dataTypes/heart-rate/dataPoints"), {"heart": [1]})

    def test_a_lost_etag_body_falls_back_to_404_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            credentials.save(tokens())
            client = FitbitClient(
                ApiSettings(),
                credentials,
                transport=ScriptedTransport(
                    [Response(200, b'{"a":1}', {"ETag": '"z"'}), Response(304, b"", {"ETag": '"z"'})]
                ),
                import_settings=ImportSettings(),
            )
            client.get("/users/me/dataTypes/steps/dataPoints")
            client.etags._entries.clear()
            with self.assertRaises(HTTPError):
                client.get("/users/me/dataTypes/steps/dataPoints")

    def test_post_form_is_used_for_the_token_endpoints(self) -> None:
        client, transport = self.client([Response(200, b'{"access_token":"x"}')])
        response = client.post_form("https://oauth2.googleapis.com/token", {"grant_type": "refresh_token"})
        self.assertTrue(response.ok)
        self.assertEqual(transport.requests[-1]["method"], "POST")

    def test_rate_limit_state_is_reportable(self) -> None:
        client, _ = self.client(
            [Response(200, b"{}", {"X-RateLimit-Limit": "150", "X-RateLimit-Remaining": "3", "X-RateLimit-Reset": "60", "X-RateLimit-Limit-Interval": "15m"})]
        )
        client.get("/users/me/dataTypes/steps/dataPoints")
        self.assertEqual(
            client.rate_limit.as_dict(),
            {"limit": 150, "remaining": 3, "reset_in": 60.0, "scope": "15m"},
        )
        self.assertEqual(RateLimit().as_dict()["limit"], None)


class AuthFlowTest(unittest.TestCase):
    def test_pkce_matches_the_rfc_test_vector(self) -> None:
        self.assertEqual(
            pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )

    def test_token_exchange_posts_the_code_and_verifier(self) -> None:
        transport = ScriptedTransport(
            [Response(200, b'{"access_token":"a","refresh_token":"r","expires_in":28800,"scope":"profile sleep","user_id":"77"}')]
        )
        result = exchange_code(
            transport,
            token_url="https://oauth2.googleapis.com/token",
            client_id="cid",
            client_secret="secret",
            code="the-code",
            redirect_uri="http://localhost:55413/",
            code_verifier="verifier",
        )
        first = transport.requests[0]
        self.assertEqual((first["method"], first["url"]), ("POST", "https://oauth2.googleapis.com/token"))
        self.assertEqual(first["headers"]["Authorization"][:6], "Basic ")
        self.assertIn(b"grant_type=authorization_code", _body(transport))
        self.assertIn(b"code_verifier=verifier", _body(transport))
        self.assertEqual((result.access_token, result.user_id, result.scopes), ("a", "77", ("profile", "sleep")))

    def test_refresh_uses_the_refresh_grant(self) -> None:
        transport = ScriptedTransport([Response(200, b'{"access_token":"a2","refresh_token":"r2","expires_in":100}')])
        result = refresh(
            transport,
            token_url="https://oauth2.googleapis.com/token",
            client_id="cid",
            client_secret="s",
            refresh_token="r1",
        )
        self.assertIn(b"grant_type=refresh_token", _body(transport))
        self.assertEqual(result.access_token, "a2")

    def test_a_refused_token_request_is_named(self) -> None:
        transport = ScriptedTransport([Response(400, b'{"errors":[{"errorType":"invalid_grant","message":"code_expired"}]}')])
        with self.assertRaises(CredentialsExpiredError) as caught:
            refresh(
                transport,
                token_url="https://oauth2.googleapis.com/token",
                client_id="c",
                client_secret="s",
                refresh_token="r",
            )
        self.assertIn("code_expired", str(caught.exception))

    def test_a_token_response_without_a_token_is_rejected(self) -> None:
        transport = ScriptedTransport([Response(200, b'{"note":"surprise"}')])
        with self.assertRaises(CredentialsExpiredError):
            refresh(transport, token_url="u", client_id="c", client_secret="s", refresh_token="r")

    def test_credential_file_is_private_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CredentialStore(Path(tmp) / "sub" / "credentials.json")
            self.assertIsNone(store.load_or_none())
            self.assertFalse(store.delete())
            store.save(tokens())
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            loaded = store.load()
            self.assertEqual(loaded.access_token, "access-1")
            self.assertFalse(loaded.needs_refresh())
            self.assertTrue(tokens(expires_at=datetime.now(UTC) - timedelta(seconds=1)).needs_refresh())
            self.assertEqual(loaded.authorization_header(), {"Authorization": "Bearer access-1"})
            self.assertTrue(store.delete())
            self.assertFalse(store.exists)

    def test_a_credential_file_without_tokens_is_not_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            store = CredentialStore(path)
            path.write_text('{"access_token": ""}')
            path.chmod(0o600)  # a private file: content is what should be judged here
            self.assertIsNone(store.load_or_none())
            path.write_text("{broken")
            path.chmod(0o600)
            self.assertIsNone(store.load_or_none())

    def test_public_view_has_no_secrets(self) -> None:
        public = tokens().public()
        self.assertNotIn("access_token", public)
        self.assertNotIn("refresh_token", public)
        self.assertEqual(public["user_id"], "42")


def _body(transport: ScriptedTransport) -> bytes:
    """The body of the last request the scripted transport saw."""
    return transport.requests[-1]["body"]


class LoopbackCallbackTest(unittest.TestCase):
    """The browser hand-off: capture the redirect, or say so cleanly."""

    def test_the_redirect_params_are_captured(self) -> None:
        import urllib.request

        callback = LoopbackCallback(host="127.0.0.1", port=0, timeout=5.0)
        result: dict[str, dict[str, str]] = {}

        def serve() -> None:
            result["params"] = callback.wait()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        while callback.port == 0:  # the server binds asynchronously
            threading.Event().wait(0.01)
        urllib.request.urlopen(
            f"http://127.0.0.1:{callback.port}{REDIRECT_PATH}?code=abc123&state=xyz", timeout=2
        ).read()
        thread.join(timeout=5)
        self.assertEqual(result["params"]["code"], "abc123")
        self.assertEqual(callback.redirect_uri(), f"http://localhost:{callback.port}{REDIRECT_PATH}")

    def test_a_refused_authorization_names_the_redirect_uri(self) -> None:
        import urllib.request

        callback = LoopbackCallback(host="127.0.0.1", port=0, timeout=5.0)

        def serve() -> None:
            try:
                callback.wait()
            except UsageError as exc:
                result.append(exc)

        result: list[UsageError] = []
        threading.Thread(target=serve, daemon=True).start()
        while callback.port == 0:
            threading.Event().wait(0.01)
        urllib.request.urlopen(f"http://127.0.0.1:{callback.port}{REDIRECT_PATH}?error=access_denied", timeout=2).read()
        for _ in range(200):
            if result:
                break
            threading.Event().wait(0.01)
        self.assertEqual(len(result), 1)
        self.assertIn(REDIRECT_PATH, str(result[0].hint))

    def test_no_browser_at_all_is_a_usage_error_not_a_crash(self) -> None:
        callback = LoopbackCallback(host="127.0.0.1", port=0, timeout=0.2)
        with self.assertRaises(UsageError) as caught:
            callback.wait()
        self.assertIn("--manual", str(caught.exception.hint))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
