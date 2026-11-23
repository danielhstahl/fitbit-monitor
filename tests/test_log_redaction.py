"""Structured logging: levels from the flags or the environment, and provable redaction.

Three promises, all of them about the log rather than about the call site that wrote a
line, because that is the property the ticket asks for:

1. **the level is chosen in one place** - ``-v``/``-vv``/``-q``/``--log-level`` on the
   command line, ``FM_LOG_LEVEL`` in the environment, and a text line that carries a
   UTC timestamp and the logger (module) name;
2. **a credential never reaches the output** - an OAuth access or refresh token, a
   client secret, or *anything read from the token store* comes out as ``[REDACTED]``,
   in the text format and in the JSON format alike. Redaction is a filter on the
   handler, so a call site cannot opt out by formatting the value itself;
3. **redaction does not eat the log** - paths, counts, dates, scopes and the ordinary
   vocabulary of an import survive untouched, or the filter gets disabled.

``tests/test_data_policy.py`` covers the sibling filter for health readings; this file
covers secrets, levels and formats.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:  # importable under pytest and under `unittest discover`
    sys.path.insert(0, str(REPO / "src"))

from fitbit_monitor import logfmt, logsecrets, logsafe  # noqa: E402
from fitbit_monitor.api.auth import CredentialStore, TokenSet  # noqa: E402
from fitbit_monitor.cli import main  # noqa: E402
from fitbit_monitor.cli import plugins_cmd  # noqa: E402
from fitbit_monitor.cli.common import configure_logging  # noqa: E402
from fitbit_monitor.config import ApiSettings, AppConfig  # noqa: E402
from fitbit_monitor.errors import UsageError  # noqa: E402
from fitbit_monitor.logfmt import JsonFormatter, TextFormatter  # noqa: E402
from fitbit_monitor.logsecrets import MASK, redact_secrets  # noqa: E402
from fitbit_monitor.plugins.contract import plugin_logger  # noqa: E402

ACCESS_TOKEN = "ya29.a0Arg7Q-HfRm0kPq0XvE0c9Z0LmABCDEF"
REFRESH_TOKEN = "1//dKQ0abcdefghij-KLMNOPqrstUVWXYZ"
CLIENT_SECRET = "9f2c1d7a4b6e8e0f3a5c7d9b1e3f5a7c"
CODE_VERIFIER = "Zm9vYmFyLXZlcmlmaWVyLTEyMzQ1Njc4OQ"

TEXT_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+([A-Z]+)\s+(\S+?): ")


class SecretRedactionTest(unittest.TestCase):
    """The pattern pass: a key that names a secret does not keep its value."""

    def test_mask_is_the_documented_placeholder(self) -> None:
        self.assertEqual(MASK, "[REDACTED]")

    def test_credential_keys_are_masked_in_every_shape(self) -> None:
        cases = {
            f"exchanging code: access_token={ACCESS_TOKEN}": f"exchanging code: access_token={MASK}",
            f"saved refresh_token = {REFRESH_TOKEN}": f"saved refresh_token = {MASK}",
            f'{{"access_token": "{ACCESS_TOKEN}", "expires_in": 28800}}': f'{{"access_token": "{MASK}", '
            '"expires_in": 28800}',
            f"config: client_secret='{CLIENT_SECRET}'": f"config: client_secret='{MASK}'",
            f"POST https://api.fitbit.com/oauth2/token?access_token={ACCESS_TOKEN}&x=1": f"POST "
            f"https://api.fitbit.com/oauth2/token?access_token={MASK}&x=1",
            f"headers: {{'Authorization': 'Bearer {ACCESS_TOKEN}', 'Accept': 'application/json'}}": f"headers: "
            f"{{'Authorization': 'Bearer {MASK}', 'Accept': 'application/json'}}",
            f"Authorization: Bearer {ACCESS_TOKEN}": f"Authorization: Bearer {MASK}",
            "Authorization: Basic VXNlcjoyYzFkN2E0YjZlOGUwZjNhNWM3ZDliMWUzZjVhN2M=": f"Authorization: Basic {MASK}",
            f"client_secret: \"{CLIENT_SECRET}\"": f'client_secret: "{MASK}"',
            f"the token is stored as token:{ACCESS_TOKEN}, done": f"the token is stored as token:{MASK}, done",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(redact_secrets(text), expected)
                self.assertNotIn(ACCESS_TOKEN, redact_secrets(text))
                self.assertNotIn(REFRESH_TOKEN, redact_secrets(text))
                self.assertNotIn(CLIENT_SECRET, redact_secrets(text))

    def test_ordinary_log_lines_survive_untouched(self) -> None:
        for text in (
            "imported 3 datasets in 41.2s",
            "rate limit: 12s wait for reset (3 left)",
            "schema version 3 (this build knows 3)",
            "kept 2026-05-01..2026-05-31 (31 days)",
            "client_secret_set: true",
            "token_url=https://api.fitbit.com/oauth2/token",
            "token: the token was renewed in 4s",
            "skipping 2 dataset(s) with no granted scope: spo2, body",
            "chunk 7 of 90 for heart_rate",
            "[ok]       store        path=/x/store.sqlite3 exists=yes bytes=1024",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_secrets(text), text)

    def test_an_empty_or_plain_string_is_returned_as_is(self) -> None:
        self.assertEqual(redact_secrets(""), "")


class RememberedSecretTest(unittest.TestCase):
    """The value pass: what this process holds is masked however it is formatted."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self._saved = logsecrets.remembered()
        logsecrets.clear()
        self.addCleanup(self._restore)
        logsecrets.remember_all((ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, CODE_VERIFIER))

    def _restore(self) -> None:
        logsecrets.clear()
        logsecrets.remember_all(self._saved)

    def test_remember_masks_a_value_with_no_shape_at_all(self) -> None:
        fresh = "a-brand-new-secret-value-nobody-else-uses"
        self.assertTrue(logsecrets.remember(fresh))
        self.assertEqual(redact_secrets(f"sent {fresh} to the API"), f"sent {MASK} to the API")
        self.assertFalse(logsecrets.remember(fresh), "remembering twice is a no-op")

    def test_the_value_pass_and_the_shape_pass_agree(self) -> None:
        """A remembered value that also matches a key shape is masked once, in either order."""
        self.assertEqual(redact_secrets(f"Authorization: Bearer {ACCESS_TOKEN}"), f"Authorization: Bearer {MASK}")
        self.assertEqual(redact_secrets(f"access_token={ACCESS_TOKEN}"), f"access_token={MASK}")

    def test_redaction_is_idempotent(self) -> None:
        """Two filters on one handler, or a value masked twice: one mask, not two."""
        cases = {
            f"Authorization: Bearer {ACCESS_TOKEN}": 1,
            f'{{"access_token": "{ACCESS_TOKEN}"}}': 1,
            f"saved access_token={ACCESS_TOKEN} refresh_token={REFRESH_TOKEN}": 2,
            f"stored {CLIENT_SECRET} in config.toml": 1,
            f"{{'Authorization': 'Bearer {ACCESS_TOKEN}'}}": 1,
        }
        for text, masks in cases.items():
            with self.subTest(text=text):
                once = redact_secrets(text)
                self.assertEqual(redact_secrets(once), once, "redaction must be idempotent")
                self.assertEqual(once.count(MASK), masks)
                for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
                    self.assertNotIn(secret, once)

    def test_short_and_empty_values_are_not_remembered(self) -> None:
        for value in ("", "abc", "x" * (logsecrets.MIN_REMEMBERED_LENGTH - 1)):
            self.assertFalse(logsecrets.remember(value))
        self.assertEqual(logsecrets.remember_all(["", None, "x" * logsecrets.MIN_REMEMBERED_LENGTH]), 1)

    def test_the_registry_is_bounded(self) -> None:
        for index in range(logsecrets.MAX_REMEMBERED + 10):
            logsecrets.remember(f"value-{index}-{'y' * 24}")
        self.assertLessEqual(len(logsecrets.remembered()), logsecrets.MAX_REMEMBERED)
        remaining = len(logsecrets.remembered())
        self.assertEqual(logsecrets.clear(), remaining)
        self.assertEqual(logsecrets.remembered(), ())

    def test_forget_stops_masking_one_value(self) -> None:
        logsecrets.remember(ACCESS_TOKEN)
        self.assertTrue(logsecrets.forget(ACCESS_TOKEN))
        self.assertFalse(logsecrets.forget(ACCESS_TOKEN))
        self.assertEqual(redact_secrets(ACCESS_TOKEN), ACCESS_TOKEN)

    def test_a_token_object_registers_its_own_secrets(self) -> None:
        tokens = self._tokens()
        self.assertIn(tokens.access_token, logsecrets.remembered())
        self.assertIn(tokens.refresh_token, logsecrets.remembered())

    def test_the_client_secret_registers_itself(self) -> None:
        settings = ApiSettings(client_id="cid", client_secret=CLIENT_SECRET)
        self.assertTrue(settings.configured)
        self.assertEqual(redact_secrets(f"using {CLIENT_SECRET}"), f"using {MASK}")

    def test_a_client_secret_read_from_the_config_file_is_masked(self) -> None:
        config = self.home / "config.toml"
        config.write_text(f'[api]\nclient_id = "cid"\nclient_secret = "{CLIENT_SECRET}"\n', encoding="utf-8")
        loaded = AppConfig.load(config)
        self.assertEqual(loaded.api.client_secret, CLIENT_SECRET)
        self.assertEqual(redact_secrets(f"config loaded: {CLIENT_SECRET}"), f"config loaded: {MASK}")
        # ... and `fm config show` still gets to say the secret is set, without saying it
        self.assertEqual(loaded.api.redacted()["client_secret_set"], True)

    def test_anything_read_from_the_token_store_is_masked(self) -> None:
        path = self.home / "credentials.json"
        store = CredentialStore(path)
        store.save(self._tokens())
        logsecrets.clear()  # as if this were a fresh process that only loaded the file
        tokens = store.load()
        for value in (tokens.access_token, tokens.refresh_token, str(tokens.as_dict())):
            with self.subTest(value=value[:16]):
                self.assertIn(MASK, redact_secrets(f"stored {value}"))
                self.assertNotIn(tokens.access_token, redact_secrets(f"stored {tokens.access_token}"))

    def test_logout_stops_masking_the_removed_tokens(self) -> None:
        store = CredentialStore(self.home / "credentials.json")
        store.save(self._tokens())
        self.assertTrue(store.delete())
        self.assertNotIn(ACCESS_TOKEN, logsecrets.remembered())

    def _tokens(self) -> TokenSet:
        return TokenSet(
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            expires_at=datetime.now(UTC) + timedelta(hours=8),
            scopes=("profile.readonly", "heart_rate.readonly"),
            user_id="1234",
        )


class RenderedLogTest(unittest.TestCase):
    """The same emit, rendered through each format: no secret survives either one."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        logsecrets.remember_all((ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, CODE_VERIFIER))
        self.addCleanup(
            lambda: [logsecrets.forget(v) for v in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, CODE_VERIFIER)]
        )
        self.logger = logging.getLogger("fitbit_monitor.test.log_redaction")
        self._saved = (list(self.logger.handlers), self.logger.propagate)
        self.logger.propagate = False
        self.addCleanup(self._restore_logger)

    def _restore_logger(self) -> None:
        self.logger.handlers, self.logger.propagate = self._saved

    def render(self, fmt: str, emit) -> str:
        """Run ``emit(logger)`` through the real formatter + filter stack, capture the text."""
        stream = io.StringIO()
        logfmt.configure(logger=self.logger, stream=stream, format=fmt, verbose=2)
        emit(self.logger)
        for handler in self.logger.handlers:
            handler.flush()
        return stream.getvalue()

    def test_a_token_in_a_text_line_comes_back_redacted(self) -> None:
        text = self.render(
            "text",
            lambda log: log.info("exchanging the code %s for access_token=%s", CODE_VERIFIER, ACCESS_TOKEN),
        )
        self.assertIn(MASK, text)
        for secret in (ACCESS_TOKEN, CODE_VERIFIER):
            self.assertNotIn(secret, text)
        match = TEXT_LINE_RE.match(text)
        self.assertIsNotNone(match, f"a text log line needs a timestamp and a logger name: {text!r}")
        self.assertEqual(match.group(1), "INFO")
        self.assertEqual(match.group(2), "fitbit_monitor.test.log_redaction")

    def test_a_token_in_a_json_line_comes_back_redacted(self) -> None:
        line = self.render(
            "json",
            lambda log: log.warning("refresh refused for refresh_token=%s", REFRESH_TOKEN),
        )
        payload = json.loads(line)
        self.assertEqual(payload["level"], "warning")
        self.assertEqual(payload["logger"], "fitbit_monitor.test.log_redaction")
        self.assertEqual(payload["module"], "test_log_redaction")
        self.assertIn(MASK, payload["message"])
        self.assertNotIn(REFRESH_TOKEN, line)

    def test_the_client_secret_and_a_token_set_are_redacted_in_both_formats(self) -> None:
        tokens = {
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "client_secret": CLIENT_SECRET,
        }
        for fmt in ("text", "json"):
            with self.subTest(fmt=fmt):
                rendered = self.render(fmt, lambda log: log.debug("payload %s", tokens))
                for secret in tokens.values():
                    self.assertNotIn(secret, rendered)
                self.assertIn(MASK, rendered)

    def test_a_health_reading_is_still_redacted_next_to_the_secrets(self) -> None:
        text = self.render("text", lambda log: log.info("resting_hr=61.3 with access_token=%s", ACCESS_TOKEN))
        self.assertIn("resting_hr=<redacted>", text)
        self.assertIn(MASK, text)

    def test_an_exception_is_redacted_in_both_formats(self) -> None:
        def emit(log: logging.Logger) -> None:
            try:
                raise ValueError(f"bad response for access_token={ACCESS_TOKEN}")
            except ValueError:
                log.error("token exchange failed", exc_info=True)

        for fmt in ("text", "json"):
            with self.subTest(fmt=fmt):
                rendered = self.render(fmt, emit)
                self.assertIn("ValueError", rendered)
                self.assertIn(MASK, rendered)
                self.assertNotIn(ACCESS_TOKEN, rendered)

    def test_extra_fields_survive_into_the_json_output(self) -> None:
        line = self.render(
            "json", lambda log: log.info("fetched", extra={"dataset": "heart_rate", "rows": 12})
        )
        payload = json.loads(line)
        self.assertEqual(payload["dataset"], "heart_rate")
        self.assertEqual(payload["rows"], 12)

    def test_a_plugin_log_line_is_redacted_like_every_other_line(self) -> None:
        """A third-party plugin's ``ctx.log`` cannot write a secret either."""
        adapter = plugin_logger("demo")
        adapter.logger.propagate = False
        self.addCleanup(setattr, adapter.logger, "propagate", True)
        stream = io.StringIO()
        logfmt.configure(logger=adapter.logger, stream=stream, format="json", verbose=2)
        adapter.error("could not refresh: access_token=%s", ACCESS_TOKEN)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["logger"], "fitbit_monitor.plugin.demo")
        self.assertEqual(payload["plugin"], "demo")
        self.assertIn(MASK, payload["message"])
        self.assertNotIn(ACCESS_TOKEN, stream.getvalue())

    def test_configuring_twice_does_not_stack_filters_or_handlers(self) -> None:
        self.render("text", lambda log: log.info("one"))
        logfmt.configure(logger=self.logger, stream=io.StringIO(), format="text", verbose=2)
        handler = self.logger.handlers[0]
        names = [getattr(f, "name", "") for f in handler.filters]
        self.assertEqual(names.count(logsecrets.FILTER_NAME), 1)
        self.assertEqual(names.count(logsafe.FILTER_NAME), 1)
        self.assertEqual(len(self.logger.handlers), 1)


class LevelAndFormatTest(unittest.TestCase):
    """``-v``/``-vv``/``-q``/``--log-level`` and ``FM_LOG_LEVEL``/``FM_LOG_FORMAT``."""

    def setUp(self) -> None:
        for name in (logfmt.LEVEL_ENV, logfmt.FORMAT_ENV):
            self._unset(name)

    def _unset(self, name: str) -> None:
        original = os.environ.get(name)
        self.addCleanup(
            lambda: os.environ.__setitem__(name, original) if original is not None else os.environ.pop(name, None)
        )
        os.environ.pop(name, None)

    def test_default_is_warning(self) -> None:
        self.assertEqual(logfmt.resolve_level(), logging.WARNING)

    def test_verbose_counts_raise_the_level(self) -> None:
        self.assertEqual(logfmt.resolve_level(verbose=1), logging.INFO)
        self.assertEqual(logfmt.resolve_level(verbose=2), logging.DEBUG)
        self.assertEqual(logfmt.resolve_level(verbose=9), logging.DEBUG, "-vvvv is -vv")

    def test_quiet_lowers_it_and_beats_a_single_v(self) -> None:
        self.assertEqual(logfmt.resolve_level(quiet=True), logging.ERROR)
        self.assertEqual(logfmt.resolve_level(verbose=1, quiet=True), logging.ERROR)

    def test_the_environment_sets_the_level_when_no_flag_does(self) -> None:
        os.environ[logfmt.LEVEL_ENV] = "debug"
        self.assertEqual(logfmt.resolve_level(), logging.DEBUG)
        os.environ[logfmt.LEVEL_ENV] = "ERROR"
        self.assertEqual(logfmt.resolve_level(), logging.ERROR)
        self.assertEqual(logfmt.resolve_level(verbose=1), logging.INFO, "a flag beats the environment")

    def test_a_bad_level_is_a_usage_error_naming_the_variable(self) -> None:
        os.environ[logfmt.LEVEL_ENV] = "loud"
        with self.assertRaises(UsageError) as caught:
            logfmt.resolve_level()
        self.assertIn(logfmt.LEVEL_ENV, str(caught.exception))
        self.assertIn("loud", str(caught.exception))

    def test_a_named_level_from_the_flag_wins_over_everything(self) -> None:
        os.environ[logfmt.LEVEL_ENV] = "error"
        self.assertEqual(logfmt.resolve_level(verbose=2, level="info"), logging.INFO)
        self.assertEqual(logfmt.parse_level("10"), logging.DEBUG)

    def test_format_comes_from_the_flag_then_the_environment(self) -> None:
        self.assertEqual(logfmt.resolve_format(), "text")
        os.environ[logfmt.FORMAT_ENV] = "json"
        self.assertEqual(logfmt.resolve_format(), "json")
        self.assertEqual(logfmt.resolve_format("text"), "text")

    def test_a_bad_format_is_a_usage_error(self) -> None:
        os.environ[logfmt.FORMAT_ENV] = "yaml"
        with self.assertRaises(UsageError):
            logfmt.resolve_format()
        with self.assertRaises(UsageError):
            logfmt.make_formatter("yaml")

    def test_formatters_render_the_same_record(self) -> None:
        record = logging.LogRecord(
            "fitbit_monitor.api.client", logging.INFO, "/x/client.py", 12, "fetched %s", ("heart_rate",), None
        )
        text = TextFormatter().format(record)
        match = TEXT_LINE_RE.match(text)
        self.assertIsNotNone(match, text)
        self.assertIn("fitbit_monitor.api.client", text)
        self.assertIn("fetched heart_rate", text)
        payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(
            (payload["level"], payload["logger"], payload["message"]),
            ("info", "fitbit_monitor.api.client", "fetched heart_rate"),
        )
        self.assertRegex(payload["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_level_name_and_helpers(self) -> None:
        self.assertEqual(logfmt.level_name(logging.INFO), "info")
        self.assertNotIn("name", logfmt.extras(logging.LogRecord("x", 1, "/x/y.py", 1, "m", (), None)))


class ConfigureLoggingTest(unittest.TestCase):
    """``configure_logging`` is what the CLI calls, on the root logger."""

    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.saved = (list(self.root.handlers), self.root.level)
        self.root.handlers = [logging.StreamHandler(io.StringIO())]
        self.stream = io.StringIO()

    def tearDown(self) -> None:
        self.root.handlers, self.root.level = self.saved

    def test_level_and_filters_are_applied_to_every_root_handler(self) -> None:
        configure_logging(2, False, stream=self.stream)
        self.assertEqual(self.root.level, logging.DEBUG)
        handler = self.root.handlers[0]
        names = [getattr(item, "name", "") for item in handler.filters]
        self.assertEqual(names.count(logsecrets.FILTER_NAME), 1)
        self.assertEqual(names.count(logsafe.FILTER_NAME), 1)
        configure_logging(0, False, stream=self.stream)
        self.assertEqual(self.root.level, logging.WARNING)

    def test_quiet_and_the_environment_are_honoured(self) -> None:
        with mock.patch.dict(os.environ, {"FM_LOG_LEVEL": "info"}):
            configure_logging(0, True, stream=self.stream)
            self.assertEqual(self.root.level, logging.ERROR)
            configure_logging(0, False, stream=self.stream)
            self.assertEqual(self.root.level, logging.INFO)

    def test_a_bad_environment_variable_is_reported_as_a_usage_error(self) -> None:
        with mock.patch.dict(os.environ, {"FM_LOG_LEVEL": "shout"}):
            with self.assertRaises(UsageError):
                configure_logging(0, False, stream=self.stream)

    def test_a_token_logged_through_the_cli_setup_is_masked(self) -> None:
        configure_logging(2, False, stream=self.stream, format="json")
        logging.getLogger("fitbit_monitor.api.auth").info("saved access_token=%s", ACCESS_TOKEN)
        for handler in self.root.handlers:
            handler.flush()
        self.assertNotIn(ACCESS_TOKEN, self.stream.getvalue())
        self.assertIn(MASK, self.stream.getvalue())


class CliLoggingTest(unittest.TestCase):
    """The flags on a real ``fm`` invocation, end to end."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        for name, value in {
            "XDG_DATA_HOME": str(self.home / "data"),
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "HOME": str(self.home),
        }.items():
            self._setenv(name, value)
        for name in (logfmt.LEVEL_ENV, logfmt.FORMAT_ENV):
            self._setenv(name, "")
        root = logging.getLogger()
        self.saved = (list(root.handlers), root.level)
        root.handlers = []  # the CLI installs its own handler on an empty root
        self.addCleanup(self._restore_root)
        logsecrets.remember_all((ACCESS_TOKEN, REFRESH_TOKEN))

        def forget_em() -> None:
            logsecrets.forget(ACCESS_TOKEN)
            logsecrets.forget(REFRESH_TOKEN)

        self.addCleanup(forget_em)

    def _setenv(self, name: str, value: str) -> None:
        original = os.environ.get(name)
        os.environ[name] = value

        def restore() -> None:
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original

        self.addCleanup(restore)

    def _restore_root(self) -> None:
        root = logging.getLogger()
        root.handlers, root.level = self.saved

    def emit(self, args, session) -> int:
        logging.getLogger("fitbit_monitor.api.client").info("retrying with access_token=%s", ACCESS_TOKEN)
        return 0

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(plugins_cmd, "cmd_plugins_list", side_effect=self.emit):
            with redirect_stdout(out), redirect_stderr(err):
                code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_quiet_by_default_prints_nothing_and_warn_level_is_silent_here(self) -> None:
        code, _, err = self.run_cli("plugins", "list")
        self.assertEqual(code, 0)
        self.assertEqual(err.strip(), "", "an info record must not appear at the default level")

    def test_v_emits_the_record_as_text_with_timestamp_and_module(self) -> None:
        code, _, err = self.run_cli("-v", "plugins", "list")
        self.assertEqual(code, 0)
        self.assertRegex(err, TEXT_LINE_RE)
        self.assertIn("fitbit_monitor.api.client", err)
        self.assertIn(MASK, err)
        self.assertNotIn(ACCESS_TOKEN, err)

    def test_log_format_json_emits_parsable_lines_and_still_redacts(self) -> None:
        code, _, err = self.run_cli("-v", "--log-format", "json", "plugins", "list")
        self.assertEqual(code, 0)
        payload = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(payload["level"], "info")
        self.assertEqual(payload["logger"], "fitbit_monitor.api.client")
        self.assertIn(MASK, payload["message"])
        self.assertNotIn(ACCESS_TOKEN, err)

    def test_fm_log_level_and_fm_log_format_are_honoured_without_flags(self) -> None:
        self._setenv(logfmt.LEVEL_ENV, "debug")
        self._setenv(logfmt.FORMAT_ENV, "json")
        code, _, err = self.run_cli("plugins", "list")
        self.assertEqual(code, 0)
        payload = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(payload["level"], "info")

    def test_a_bad_fm_log_level_is_a_usage_error_not_a_traceback(self) -> None:
        self._setenv(logfmt.LEVEL_ENV, "shout")
        code, _, err = self.run_cli("plugins", "list")
        self.assertEqual(code, 2)
        self.assertIn("FM_LOG_LEVEL", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
