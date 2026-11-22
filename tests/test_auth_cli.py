"""CLI tests for ``fm auth`` and ``fm config``.

The browser half of OAuth cannot be automated, but everything after the redirect can:
``--fixtures`` replays the token exchange, so the login command's storage, its status
reporting, and its logout are all exercised for real. ``fm config`` is tested the same
way it is used - by running it.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_cli import CliTestCase

from import_support import FIXTURES, write_credentials
from fitbit_monitor.api.auth import CredentialStore
from fitbit_monitor.cli.common import EXIT_USAGE
from fitbit_monitor.ingest.report import EXIT_OK


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
        self.assertIn("heartbeat", payload["scopes"])
        self.assertFalse(payload["expired"])
        self.assertAlmostEqual(payload["seconds_left"], tokens.seconds_left(), delta=60)
        self.assertNotIn("access_token", json.dumps(payload), "the public view has no secrets")

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

    def test_login_is_refused_before_a_client_id_exists(self) -> None:
        code, text = self.fm("auth", "login", "--manual", "x")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("client_id", text)

    def _configure_app(self, *, token_url: str | None = None) -> None:
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        text = '[api]\nclient_id = "test-client"\nclient_secret = "test-secret"\n'
        if token_url:
            text += f'token_url = "{token_url}"\n'
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

    def test_show_names_the_file_that_failed_to_parse(self) -> None:
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[import\n timezone ????")
        code, text = self.fm("config", "show")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("config", text.lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
