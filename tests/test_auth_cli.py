"""CLI tests for ``fm auth`` and ``fm config``.

The browser half of OAuth cannot be automated, but everything after the redirect can:
``--fixtures`` replays the token exchange, so the login command's storage, its status
reporting, and its logout are all exercised for real. ``fm config`` is tested the same
way it is used - by running it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_cli import CliTestCase

from import_support import FIXTURES, write_credentials
from fitbit_monitor.api.auth import CredentialStore, PkceFlow, TokenSet
from fitbit_monitor.api.catalog import ACTIVITY_SCOPE, ALL_SCOPES, HEALTH_METRICS_SCOPE
from fitbit_monitor.cli.common import EXIT_USAGE
from fitbit_monitor.errors import UsageError
from fitbit_monitor.ingest.report import EXIT_OK


class ConsentUrlScopeGuardTest(unittest.TestCase):
    """The last line of defence is the one that builds the URL.

    ``ApiSettings`` resolves scope names on load, but ``PkceFlow`` can be handed any list -
    a tool, a script, a future caller - and Google's response to a scope it does not know
    is a 400 that explains nothing to the person staring at it.
    """

    def test_a_scope_that_is_not_googles_never_reaches_google(self) -> None:
        flow = PkceFlow.new()
        with self.assertRaises(UsageError) as caught:
            flow.authorize_url(client_id="c", redirect_uri="http://localhost:1/", scopes=("spo2", ACTIVITY_SCOPE))
        self.assertIn("spo2", str(caught.exception))
        self.assertIn("config init", caught.exception.hint or "")

    def test_google_scopes_build_a_url(self) -> None:
        url = PkceFlow.new().authorize_url(client_id="c", redirect_uri="http://localhost:1/", scopes=(ACTIVITY_SCOPE,))
        self.assertIn("scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgooglehealth.activity_and_fitness.readonly", url)


class TokenEndpoint:
    """A stand-in for Fitbit's token endpoint, so login can be tested for real."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(
                    {"path": self.path, "body": self.rfile.read(length).decode(), "auth": self.headers.get("Authorization", "")}
                )
                body = json.dumps(outer.payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/oauth2/token"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class CredentialPermissionsTest(unittest.TestCase):
    """The token file is the most sensitive thing this program writes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "credentials.json"
        self.store = CredentialStore(self.path)

    def tokens(self) -> TokenSet:
        return TokenSet(
            access_token="secret-access",
            refresh_token="secret-refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=8),
            scopes=("activity",),
            user_id="42",
        )

    def test_save_is_owner_only(self) -> None:
        self.store.save(self.tokens())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_a_world_readable_token_file_is_refused_with_a_fix_line(self) -> None:
        self.store.save(self.tokens())
        os.chmod(self.path, 0o644)
        with self.assertRaises(UsageError) as caught:
            self.store.load()
        self.assertIn("readable by more than you", str(caught.exception))
        self.assertIn("chmod 600", str(caught.exception.hint))
        with self.assertRaises(UsageError):  # a loose mode is loud, not silently "not authenticated"
            self.store.load_or_none()
        os.chmod(self.path, 0o600)
        self.assertEqual(self.store.load().access_token, "secret-access")

    def test_nothing_about_the_tokens_reaches_the_logs(self) -> None:
        import logging

        self.store.save(self.tokens())
        with self.assertLogs("fitbit_monitor", level="DEBUG") as captured:
            logging.getLogger("fitbit_monitor.api.auth").debug("stored credentials for user 42")
        self.assertNotIn("secret-access", "\n".join(captured.output))
        self.assertNotIn("secret-refresh", "\n".join(captured.output))

    def test_the_public_view_has_no_secrets(self) -> None:
        public = json.dumps(self.tokens().public())
        self.assertNotIn("secret-access", public)
        self.assertNotIn("secret-refresh", public)


class AuthCliTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.credentials_path = self.home / "data" / "fitbit-monitor" / "credentials.json"
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)

    def test_status_without_credentials_says_login(self) -> None:
        code, text = self.fm("auth", "status", expect=EXIT_OK)
        self.assertIn("fm auth login", text)

    def test_status_json_reports_scopes_and_expiry(self) -> None:
        tokens = write_credentials(self.credentials_path)
        code, text = self.fm("auth", "status", "--json", expect=EXIT_OK)
        payload = json.loads(text)
        self.assertTrue(payload["authenticated"])
        # The JSON is the verbatim view: scope URLs exactly as Google spells them.
        self.assertEqual(set(payload["scopes"]), set(ALL_SCOPES))
        self.assertFalse(payload["expired"])
        self.assertAlmostEqual(payload["seconds_left"], tokens.seconds_left(), delta=60)
        self.assertNotIn("access_token", json.dumps(payload), "the public view has no secrets")

    def test_status_prints_scopes_short_enough_to_read(self) -> None:
        """Four 70-character URLs on one line is not a status line."""
        write_credentials(self.credentials_path)
        _code, text = self.fm("auth", "status", expect=EXIT_OK)
        self.assertIn("health_metrics_and_measurements.readonly", text)
        self.assertNotIn("https://www.googleapis.com/auth/googlehealth", text)

    def test_status_says_when_the_token_needs_a_refresh(self) -> None:
        write_credentials(self.credentials_path, expires_in=120)
        _, text = self.fm("auth", "status", expect=EXIT_OK)
        self.assertIn("refreshes it automatically", text)

    def test_logout_removes_the_file_and_says_so_twice(self) -> None:
        write_credentials(self.credentials_path)
        code, text = self.fm("auth", "logout", expect=EXIT_OK)
        self.assertFalse(self.credentials_path.exists())
        self.assertIn("removed", text.lower())
        _code, text = self.fm("auth", "logout", expect=EXIT_OK)
        self.assertIn("nothing to remove", text)

    def test_login_with_a_pasted_code_stores_a_usable_token(self) -> None:
        tokens = TokenEndpoint(
            {
                "access_token": "fresh-access",
                "refresh_token": "fresh-refresh",
                "expires_in": 28800,
                "token_type": "Bearer",
                "user_id": "42",
                "scope": "activity profile sleep heartbeat",
            }
        )
        self.addCleanup(tokens.close)
        self._configure_app(token_url=tokens.url)
        code, text = self.fm("auth", "login", "--manual", "the-code", expect=EXIT_OK)
        self.assertIn("stored tokens", text.lower())
        self.assertIn("user 42", text)
        stored = CredentialStore(self.credentials_path).load()
        self.assertEqual(stored.access_token, "fresh-access")
        self.assertEqual(stored.scopes, ("activity", "profile", "sleep", "heartbeat"))
        self.assertGreater(stored.seconds_left(), 28000)
        # The exchange is HTTP Basic with client_id:client_secret, form-encoded body.
        import base64

        request = tokens.requests[-1]
        self.assertEqual(request["auth"], "Basic " + base64.b64encode(b"test-client:test-secret").decode())
        self.assertIn("grant_type=authorization_code", request["body"])
        self.assertIn("code=the-code", request["body"])
        self.assertIn("code_verifier=", request["body"]), "PKCE (RFC 7636)"
        # ... and the stored token is what the import path reads.
        self.assertEqual(self.fm("import", "--fixtures", str(FIXTURES), "--dry-run")[0], EXIT_OK)

    def test_login_re_asks_consent_only_when_the_stored_grant_differs(self) -> None:
        """``prompt=consent`` is for the logins that need it, not every login.

        Google hands out a refresh token at consent and not on a later silent
        authorisation, so forcing the screen every time would be the difference between a
        tool that renews and one that quietly gets an hour token - but it is also the
        "sign in with Google" screen people are taught to be careful about. So: forced when
        the configured scopes are not what is granted, or when there is no refresh token;
        silent otherwise.
        """
        tokens = TokenEndpoint({"access_token": "a", "refresh_token": "r", "expires_in": 28800})
        self.addCleanup(tokens.close)
        self._configure_app(token_url=tokens.url)

        write_credentials(self.credentials_path, scopes=(HEALTH_METRICS_SCOPE,))
        _code, text = self.fm("auth", "login", "--manual", "the-code", expect=EXIT_OK)
        self.assertIn("prompt=consent", text, "a grant for one scope cannot gain three silently")
        self.assertIn("re-asking consent", text, "and the user is told why the screen is back")

        write_credentials(self.credentials_path)
        _code, text = self.fm("auth", "login", "--manual", "the-code", expect=EXIT_OK)
        self.assertNotIn("prompt=consent", text, "same scopes, same grant: no extra screen")

    def test_login_is_refused_before_a_client_id_exists(self) -> None:
        code, text = self.fm("auth", "login", "--manual", "x")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("client_id", text)

    def test_a_config_that_predates_the_google_api_is_translated_out_loud(self) -> None:
        """``fm config init`` used to ship Fitbit's scope names; they are not Google's.

        The six names ``activity heart_profile heartbeat profile sleep spo2`` sent to
        ``accounts.google.com`` come back as ``Error 400: invalid_scope, Request details:
        flowName=GeneralOAuthFlow`` - after the password, in a browser tab, saying nothing
        a user can act on. So the config is translated on load, the consent URL carries only
        ``googlehealth.*``, and the rewrite is announced rather than silent.
        """
        tokens = TokenEndpoint({"access_token": "a", "refresh_token": "r", "expires_in": 28800})
        self.addCleanup(tokens.close)
        self._configure_app(
            token_url=tokens.url,
            scopes='["activity", "heart_profile", "heartbeat", "profile", "sleep", "spo2"]',
        )
        _code, text = self.fm("auth", "login", "--manual", "the-code", expect=EXIT_OK)
        url = next(line for line in text.splitlines() if "scope=" in line)
        for retired in ("spo2", "heart_profile", "heartbeat", "scope%3Dactivity%20"):
            self.assertNotIn(retired, url, f"{retired} is not a Google scope")
        for scope in ("health_metrics_and_measurements", "activity_and_fitness", "sleep.readonly", "profile.readonly"):
            self.assertIn(scope, url, "the grant asks for what this build reads")
        self.assertIn('"spo2" was a Fitbit scope; it is now health_metrics_and_measurements.readonly', text)
        self.assertIn('"activity" was a Fitbit scope; it is now activity_and_fitness.readonly', text)

    def test_a_scope_google_cannot_grant_stops_before_the_browser(self) -> None:
        """Something unmappable is a config error in a terminal, not a 400 in a browser."""
        self._configure_app(
            scopes='["spo2", "nutrition", "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.writeonly"]',
        )
        code, text = self.fm("auth", "login", "--manual", "x", expect=EXIT_USAGE)
        self.assertIn("nutrition", text, "the line that cannot be asked for is named")
        self.assertIn("writeonly", text)
        self.assertIn("config init", text, "and the way to fix it is printed")
        self.assertNotIn("open this URL", text, "no consent request goes out with a bad scope in it")

    def _configure_app(self, *, token_url: str | None = None, scopes: str | None = None) -> None:
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        text = '[api]\nclient_id = "test-client"\nclient_secret = "test-secret"\n'
        if token_url:
            text += f'token_url = "{token_url}"\n'
        if scopes:
            text += f"scopes = {scopes}\n"
        config.write_text(text)

    def test_login_help_documents_the_headless_path(self) -> None:
        import argparse

        from fitbit_monitor.cli import build_parser

        parser = build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        auth = sub.choices["auth"]
        login = next(a for a in auth._actions if isinstance(a, argparse._SubParsersAction)).choices["login"]
        text = login.format_help()
        self.assertIn("--manual", text)
        self.assertIn("--no-browser", text)


class ConfigCliTest(CliTestCase):
    def test_show_without_a_config_file_reports_defaults(self) -> None:
        code, text = self.fm("config", "show", expect=EXIT_OK)
        self.assertIn("not created", text)
        self.assertIn("http://localhost:", text, "the redirect URI to register is printed")
        self.assertIn("timezone=UTC", text)

    def test_init_writes_a_file_once(self) -> None:
        code, text = self.fm("config", "init", expect=EXIT_OK)
        path = self.home / "config" / "fitbit-monitor" / "config.toml"
        self.assertTrue(path.exists(), text)
        self.assertIn("client_id", path.read_text())
        self.assertEqual(self.fm("config", "init")[0], EXIT_USAGE, "refuses to overwrite")
        self.assertEqual(self.fm("config", "init", "--force")[0], EXIT_OK)

    def test_init_print_instead_of_writing(self) -> None:
        self.assertEqual(self.fm("config", "init", "--print", expect=EXIT_OK)[0], EXIT_OK)
        self.assertFalse((self.home / "config" / "fitbit-monitor" / "config.toml").exists())

    def test_show_reflects_the_file(self) -> None:
        self.fm("config", "init", expect=EXIT_OK)
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.write_text(config.read_text().replace('timezone = "UTC"', 'timezone = "Europe/Berlin"'))
        code, text = self.fm("config", "show", "--json", expect=EXIT_OK)
        payload = json.loads(text)
        self.assertEqual(payload["import"]["timezone"], "Europe/Berlin")
        self.assertIn("Europe/Berlin", self.fm("config", "show")[1])

    def test_path_prints_every_resolved_location(self) -> None:
        code, text = self.fm("config", "path", expect=EXIT_OK)
        for key in ("config", "data_dir", "store", "credentials", "imports"):
            self.assertIn(key, text)
        self.assertIn(str(self.home), text)
        _code, text = self.fm("config", "path", "--json", expect=EXIT_OK)
        self.assertTrue(json.loads(text)["store"].endswith("store.sqlite3"))

    def test_config_init_file_is_owner_only(self) -> None:
        self.fm("config", "init", expect=EXIT_OK)
        path = self.home / "config" / "fitbit-monitor" / "config.toml"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600, "it holds the client secret")

    def test_show_names_the_file_that_failed_to_parse(self) -> None:
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[import\n timezone ????")
        code, text = self.fm("config", "show")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("config", text.lower())

    def test_config_commands_still_run_on_scopes_the_api_cannot_be_asked_for(self) -> None:
        """A broken ``scopes`` line must not lock the user out of fixing it.

        ``fm auth login`` refuses to dial consent with a scope Google will reject, but the
        commands that report and repair the file load it too - so they keep working, say
        what is wrong, and ``config init --force`` writes a file whose scopes this build can
        actually ask for. Same rule as a retired ``base_url``.
        """
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[api]\nclient_id = "cid"\nclient_secret = "s"\nscopes = ["nutrition"]\n')

        _code, text = self.fm("config", "show", expect=EXIT_OK)
        self.assertIn("nutrition", text)
        self.assertIn("no replacement", text)

        _code, text = self.fm("config", "init", "--force", expect=EXIT_OK)
        self.assertIn("nutrition", text, "the rewrite says what it dropped")
        written = config.read_text()
        self.assertNotIn("nutrition", written)
        self.assertIn("googlehealth.activity_and_fitness.readonly", written)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
