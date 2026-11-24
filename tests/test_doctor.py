"""``fm doctor``: one line per check, and an exit code that means something.

The contract this file holds:

* the report is **complete** - config path, data-directory writability, token present or
  expired, store present with its schema version, plugin directories and count,
  versions, and clock sanity are all in it, in text and in ``--json``;
* the report is **stable** - one line per check, a bracketed verdict first, ``key=value``
  facts after, and the same fields in the same shape every run, because a user greps it
  and a script parses it;
* the exit code is **nonzero only on a hard failure** - a laptop that has never logged in
  is a warning, a store file that is not a store is a failure;
* a check that crashes is reported, not propagated.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:  # importable under pytest and under `unittest discover`
    sys.path.insert(0, str(REPO / "src"))

from fitbit_monitor.api.auth import CredentialStore, TokenSet  # noqa: E402
from fitbit_monitor.api.catalog import ALL_SCOPES, HEALTH_METRICS_SCOPE, SLEEP_SCOPE  # noqa: E402
from fitbit_monitor.cli import main  # noqa: E402
from fitbit_monitor.cli.common import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Session  # noqa: E402
from fitbit_monitor.cli.doctor_cmd import (  # noqa: E402
    FAIL,
    OK,
    SKIPPED,
    WARN,
    Check,
    _check_names,
    run_checks,
    summary,
)
from fitbit_monitor.config import AppConfig  # noqa: E402
from fitbit_monitor.paths import Paths  # noqa: E402
from fitbit_monitor.store import schema  # noqa: E402

#: Every check the acceptance criteria name, by the name the report prints.
REQUIRED_CHECKS = {"version", "config", "data_dir", "token", "store", "plugins", "clock", "imports"}

#: The facts the ticket names, and the check that has to carry them.
REQUIRED_FACTS = {
    "config": {"path"},
    "data_dir": {"path", "writable"},
    "token": {"present", "expired"},
    "store": {"exists", "schema_version"},
    "plugins": {"dirs", "count"},
    "version": {"fm", "python"},
    "clock": {"utc", "local_offset"},
}


class DoctorTestCase(unittest.TestCase):
    """A throwaway HOME, and a ``cli()`` that returns (code, stdout, stderr)."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.data_dir = self.home / "data" / "fitbit-monitor"
        self.config_path = self.home / "config" / "fitbit-monitor" / "config.toml"
        self.store_path = self.data_dir / "store.sqlite3"
        self.credentials_path = self.data_dir / "credentials.json"
        for name, value in {
            "XDG_DATA_HOME": str(self.home / "data"),
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "HOME": str(self.home),
        }.items():
            original = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(
                lambda o=original, k=name: os.environ.__setitem__(k, o)
                if o is not None
                else os.environ.pop(k, None)
            )

    # --- the CLI -------------------------------------------------------------
    def cli(self, *argv: str, expect: int | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        if expect is not None:
            self.assertEqual(code, expect, f"`fm {' '.join(argv)}`: {out.getvalue()}{err.getvalue()}")
        return code, out.getvalue(), err.getvalue()

    def doctor_json(self, *extra: str) -> dict:
        _, text, _ = self.cli("doctor", "--json", *extra, expect=None)
        return json.loads(text)

    # --- fixtures ------------------------------------------------------------
    def write_token(
        self,
        *,
        expires_in: float = 8 * 3600,
        mode: int | None = 0o600,
        body: str | None = None,
        scopes: tuple[str, ...] = ("profile.readonly", "heart_rate.readonly"),
    ) -> TokenSet:
        tokens = TokenSet(
            access_token="access-token-value-for-doctor-tests",
            refresh_token="refresh-token-value-for-doctor-tests",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=scopes,
            user_id="4242",
        )
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        if body is not None:
            self.credentials_path.write_text(body, encoding="utf-8")
        else:
            CredentialStore(self.credentials_path).save(tokens)
        if mode is not None:
            os.chmod(self.credentials_path, mode)
        return tokens

    def make_store(self, *, version: int | None = None) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        schema.init_db(self.store_path)
        if version is not None:
            connection = sqlite3.connect(self.store_path)
            connection.execute(
                "INSERT INTO fm_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(version),),
            )
            connection.commit()
            connection.close()
        return self.store_path

    def seed_rows(self, *timestamps: datetime) -> None:
        connection = sqlite3.connect(self.store_path)
        for moment in timestamps:
            connection.execute(
                "INSERT OR REPLACE INTO series (dataset, ts_utc, payload) VALUES (?, ?, ?)",
                ("heart_rate", moment.isoformat(), '{"heart_rate": 60}'),
            )
        connection.commit()
        connection.close()

    def record_run(self, *, status: str = "ok", quarantined: int = 0) -> None:
        connection = sqlite3.connect(self.store_path)
        connection.execute(
            "INSERT INTO dataset_run (run_id, dataset, source, since, until, status, rows_inserted,"
            " quarantined, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                "heart_rate",
                "api",
                "2026-05-01",
                "2026-05-31",
                status,
                100,
                quarantined,
                "2026-05-31T10:00:00+00:00",
                "2026-05-31T10:02:00+00:00",
            ),
        )
        connection.commit()
        connection.close()

    def broken_plugin(self) -> Path:
        directory = self.home / "plugins"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "nope.py").write_text('raise RuntimeError("nope")\n', encoding="utf-8")
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(f'[plugins]\ndirs = ["{directory}"]\n', encoding="utf-8")
        return directory

    # --- the shape of the report --------------------------------------------
    def test_every_required_check_reports(self) -> None:
        payload = self.doctor_json()
        checks = {check["check"]: check for check in payload["checks"]}
        self.assertEqual(REQUIRED_CHECKS - set(checks), set(), "fm doctor is missing a check")
        for name, facts in REQUIRED_FACTS.items():
            with self.subTest(check=name):
                self.assertTrue(facts <= set(checks[name]), f"{name} does not report {facts - set(checks[name])}")

    def test_exit_zero_and_verdicts_on_a_never_configured_install(self) -> None:
        code, text, _ = self.cli("doctor", expect=EXIT_OK)
        self.assertIn("no hard failures", text)
        verdicts = {line.split("]", 1)[0][1:]: line for line in text.splitlines() if line.startswith("[")}
        self.assertIn(WARN, verdicts, "a machine with no token should warn, not fail")
        self.assertNotIn(FAIL, verdicts)

    def test_text_lines_are_grep_friendly_and_stable(self) -> None:
        _, text, _ = self.cli("doctor", expect=EXIT_OK)
        lines = [line for line in text.splitlines() if line.startswith("[")]
        self.assertGreaterEqual(len(lines), len(REQUIRED_CHECKS))
        for line in lines:
            verdict = line.split("]")[0][1:]
            self.assertIn(verdict, (OK, WARN, FAIL, SKIPPED), line)
            self.assertRegex(line, r"^\[(ok|warn|fail|skipped)\]\s+\S+")
            self.assertNotIn("  =", line)
        # the same report twice: only the timestamp moves
        _, again, _ = self.cli("doctor", expect=EXIT_OK)
        self.assertEqual(len(lines), len([line for line in again.splitlines() if line.startswith("[")]))

    def test_json_shape_is_stable_and_self_consistent(self) -> None:
        payload = self.doctor_json()
        for key in ("tool", "report_version", "generated", "quick", "exit_code", "summary", "checks"):
            self.assertIn(key, payload)
        self.assertEqual(payload["tool"], "fm doctor")
        self.assertEqual(payload["exit_code"], EXIT_OK)
        tally = payload["summary"]
        self.assertEqual(tally["checks"], len(payload["checks"]))
        self.assertEqual(sum(tally[key] for key in (OK, WARN, FAIL, SKIPPED)), tally["checks"])
        for check in payload["checks"]:
            self.assertEqual({"check", "status", "hint"} <= set(check), True)
            self.assertIn(check["status"], (OK, WARN, FAIL, SKIPPED))

    def test_quick_skips_the_store_scans(self) -> None:
        self.make_store()
        self.seed_rows(datetime.now(UTC) + timedelta(days=1))
        full = self.doctor_json()
        quick = self.doctor_json("--quick")
        by_name = {check["check"]: check for check in full["checks"]}
        by_quick = {check["check"]: check for check in quick["checks"]}
        self.assertEqual(by_name["store"]["integrity"], "ok")
        self.assertEqual(by_quick["store"]["integrity"], "skipped")
        self.assertEqual(by_quick["clock"]["future_rows"], "skipped")
        self.assertTrue(by_name["clock"]["future_rows"])

    def test_check_filter_runs_one_check_and_rejects_a_stranger(self) -> None:
        code, text, _ = self.cli("doctor", "--check", "version", expect=EXIT_OK)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("[")]), 1)
        self.assertIn("version", text)
        code, text, _ = self.cli("doctor", "--check", "nonexistent")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no such check", text)
        payload = self.doctor_json("--check", "version", "--check", "config")
        self.assertEqual([check["check"] for check in payload["checks"]], ["version", "config"])

    # --- config --------------------------------------------------------------
    def test_config_path_is_reported_and_a_starter_file_is_suggested(self) -> None:
        payload = self.doctor_json()
        config = next(check for check in payload["checks"] if check["check"] == "config")
        self.assertEqual(config["path"], str(self.config_path))
        self.assertFalse(config["exists"])
        self.assertIn("config init", config["hint"])

    def test_a_world_readable_config_with_a_secret_warns(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text('[api]\nclient_id = "cid"\nclient_secret = "a-real-looking-secret"\n')
        os.chmod(self.config_path, 0o644)
        config = next(check for check in self.doctor_json()["checks"] if check["check"] == "config")
        self.assertEqual(config["status"], WARN)
        self.assertIn("chmod 600", config["hint"])

    # --- data dir ------------------------------------------------------------
    def test_missing_but_creatable_data_dir_is_fine(self) -> None:
        data_dir = next(check for check in self.doctor_json()["checks"] if check["check"] == "data_dir")
        self.assertFalse(data_dir["exists"])
        self.assertTrue(data_dir["creatable"])
        self.assertEqual(data_dir["status"], OK)

    def test_an_unwritable_data_dir_is_a_hard_failure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o500)  # r-x: nothing can be created inside it
        try:
            code, text, _ = self.cli("doctor")
            self.assertEqual(code, EXIT_ERROR)
            self.assertIn("data_dir", text)
            payload = self.doctor_json()
            data_dir = next(check for check in payload["checks"] if check["check"] == "data_dir")
            self.assertEqual(data_dir["status"], FAIL)
            self.assertFalse(data_dir["writable"])
            self.assertEqual(payload["exit_code"], EXIT_ERROR)
        finally:
            os.chmod(self.data_dir, 0o755)

    # --- token ---------------------------------------------------------------
    def test_the_token_check_prints_scopes_short_enough_to_read(self) -> None:
        """Four 70-character URLs is not a diagnostic line.

        The short names are what you act on (`sleep.readonly` is missing); the URLs stay
        available, verbatim, for whatever reads `fm doctor --json`.
        """
        self.write_token(scopes=(HEALTH_METRICS_SCOPE, SLEEP_SCOPE))
        token = next(check for check in self.doctor_json()["checks"] if check["check"] == "token")
        self.assertEqual(
            token["scopes"], ["health_metrics_and_measurements.readonly", "sleep.readonly"]
        )
        self.assertNotIn("googleapis.com", json.dumps(token["scopes"]))
        self.assertEqual(token["scope_urls"], [HEALTH_METRICS_SCOPE, SLEEP_SCOPE])

    def test_no_token_warns_and_says_what_to_run(self) -> None:
        token = next(check for check in self.doctor_json()["checks"] if check["check"] == "token")
        self.assertEqual(token["status"], WARN)
        self.assertFalse(token["present"])
        self.assertIn("fm auth login", token["hint"])

    def test_a_valid_token_is_ok_and_reports_its_expiry(self) -> None:
        self.write_token()
        token = next(check for check in self.doctor_json()["checks"] if check["check"] == "token")
        self.assertEqual(token["status"], OK)
        self.assertTrue(token["present"])
        self.assertFalse(token["expired"])
        self.assertFalse(token["needs_refresh"])
        self.assertEqual(token["user_id"], "4242")
        self.assertGreater(token["seconds_left"], 3600)

    def test_an_expired_token_warns_without_failing_the_run(self) -> None:
        self.write_token(expires_in=-60)
        code, _, _ = self.cli("doctor", expect=EXIT_OK)
        token = next(check for check in self.doctor_json()["checks"] if check["check"] == "token")
        self.assertEqual(token["status"], WARN)
        self.assertTrue(token["expired"])
        self.assertEqual(code, EXIT_OK)

    def test_a_group_readable_token_is_a_hard_failure(self) -> None:
        self.write_token(mode=0o644)
        code, _, _ = self.cli("doctor", expect=EXIT_ERROR)
        token = next(check for check in self.doctor_json()["checks"] if check["check"] == "token")
        self.assertEqual(token["status"], FAIL)
        self.assertIn("chmod 600", token["hint"])
        self.assertEqual(code, EXIT_ERROR)

    def test_an_unreadable_token_file_is_a_hard_failure(self) -> None:
        self.write_token(body="{not json")
        _, text, _ = self.cli("doctor", expect=EXIT_ERROR)
        self.assertIn("token", text)

    # --- store ---------------------------------------------------------------
    def test_no_store_warns_and_names_two_ways_to_make_one(self) -> None:
        store = next(check for check in self.doctor_json()["checks"] if check["check"] == "store")
        self.assertEqual(store["status"], WARN)
        self.assertFalse(store["exists"])
        self.assertIn("fm db init", store["hint"])

    def test_a_seeded_store_is_ok_and_reports_the_schema_version(self) -> None:
        self.make_store()
        store = next(check for check in self.doctor_json()["checks"] if check["check"] == "store")
        self.assertEqual(store["status"], OK)
        self.assertEqual(store["schema_version"], schema.SCHEMA_VERSION)
        self.assertEqual(store["schema_known"], schema.SCHEMA_VERSION)
        self.assertTrue(store["up_to_date"])
        self.assertEqual(store["integrity"], "ok")
        self.assertGreater(store["tables"], 5)

    def test_an_out_of_date_store_warns_with_the_command(self) -> None:
        self.make_store(version=1)
        store = next(check for check in self.doctor_json()["checks"] if check["check"] == "store")
        self.assertEqual(store["status"], WARN)
        self.assertEqual(store["schema_version"], 1)
        self.assertIn("fm db migrate", store["hint"])

    def test_a_store_from_a_newer_fm_is_a_hard_failure(self) -> None:
        self.make_store(version=schema.SCHEMA_VERSION + 99)
        code, _, _ = self.cli("doctor", expect=EXIT_ERROR)
        store = next(check for check in self.doctor_json()["checks"] if check["check"] == "store")
        self.assertEqual(store["status"], FAIL)
        self.assertIn("newer", store["hint"])
        self.assertEqual(code, EXIT_ERROR)

    def test_a_file_that_is_not_a_database_is_a_hard_failure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text("this is not a database, it is a shopping list\n" * 4, encoding="utf-8")
        code, _, _ = self.cli("doctor", expect=EXIT_ERROR)
        store = next(check for check in self.doctor_json()["checks"] if check["check"] == "store")
        self.assertEqual(store["status"], FAIL)
        self.assertIn("not a SQLite database", store["error"])
        self.assertEqual(code, EXIT_ERROR)

    def test_an_empty_file_is_a_warning_not_a_failure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store_path.touch()
        _, text, _ = self.cli("doctor", expect=EXIT_OK)
        self.assertIn("schema_version=-", text)

    # --- plugins -------------------------------------------------------------
    def test_plugin_directories_and_count_are_reported(self) -> None:
        plugins = next(check for check in self.doctor_json()["checks"] if check["check"] == "plugins")
        self.assertEqual(plugins["dirs"], ["-"])
        self.assertGreaterEqual(plugins["count"], 1, "the built-in plugins should be counted")

    def test_a_broken_plugin_warns_and_names_it(self) -> None:
        self.broken_plugin()
        code, text, _ = self.cli("doctor", expect=EXIT_OK)
        plugins = next(check for check in self.doctor_json()["checks"] if check["check"] == "plugins")
        self.assertEqual(plugins["status"], WARN)
        self.assertIn("nope", plugins["broken"])
        self.assertEqual(plugins["usable"], plugins["count"] - 1)
        self.assertIn("fm plugins list", text)

    def test_a_missing_plugin_directory_warns(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(f'[plugins]\ndirs = ["{self.home / "nowhere"}"]\n', encoding="utf-8")
        plugins = next(check for check in self.doctor_json()["checks"] if check["check"] == "plugins")
        self.assertEqual(plugins["status"], WARN)
        self.assertEqual(plugins["dirs_missing"], [str(self.home / "nowhere")])

    # --- clock ---------------------------------------------------------------
    def test_a_future_row_is_a_clock_warning(self) -> None:
        self.make_store()
        self.seed_rows(datetime.now(UTC) + timedelta(days=2))
        code, _, _ = self.cli("doctor", expect=EXIT_OK)
        clock = next(check for check in self.doctor_json()["checks"] if check["check"] == "clock")
        self.assertEqual(clock["status"], WARN)
        self.assertEqual(clock["future_rows"], 1)
        self.assertEqual(code, EXIT_OK)

    def test_a_clock_out_of_all_reason_reports_a_failure(self) -> None:
        checks = run_checks(self.session(), now=datetime(1999, 1, 1, tzinfo=UTC))
        clock = next(check for check in checks if check.name == "clock")
        self.assertEqual(clock.status, FAIL)
        self.assertIn("1999", clock.hint)

    # --- imports -------------------------------------------------------------
    def test_no_store_skips_the_import_check(self) -> None:
        imports = next(check for check in self.doctor_json()["checks"] if check["check"] == "imports")
        self.assertEqual(imports["status"], SKIPPED)

    def test_a_clean_run_is_ok_and_a_failed_one_warns(self) -> None:
        self.make_store()
        self.record_run(status="ok")
        imports = next(check for check in self.doctor_json()["checks"] if check["check"] == "imports")
        self.assertEqual(imports["status"], OK)
        self.assertEqual(imports["run_id"], "run-1")
        self.record_run(status="failed", quarantined=2)
        imports = next(check for check in self.doctor_json()["checks"] if check["check"] == "imports")
        self.assertEqual(imports["status"], WARN)
        self.assertIn("fm import history", imports["hint"])

    # --- internals -----------------------------------------------------------
    def session(self) -> Session:
        return Session(paths=Paths.resolve(), config=AppConfig.load(missing_ok=True))

    def test_summary_counts_and_lines(self) -> None:
        checks = [Check("a", OK), Check("b", WARN, {"x": 1}), Check("c", FAIL), Check("d", SKIPPED)]
        tally = summary(checks)
        self.assertEqual(tally, {"checks": 4, OK: 1, WARN: 1, FAIL: 1, SKIPPED: 1})
        self.assertEqual(checks[1].line().split()[0], "[warn]")
        self.assertIn("x=1", checks[1].line())

    def test_a_check_that_raises_becomes_a_failure_naming_itself(self) -> None:
        from fitbit_monitor.cli import doctor_cmd

        def explode(ctx: doctor_cmd.Context) -> Check:
            raise RuntimeError("boom")

        saved = doctor_cmd.CHECKS
        doctor_cmd.CHECKS = (explode,)
        try:
            checks = run_checks(self.session())
        finally:
            doctor_cmd.CHECKS = saved
        self.assertEqual(checks[0].name, "explode")
        self.assertEqual(checks[0].status, FAIL)
        self.assertIn("boom", checks[0].detail["error"])

    def test_every_check_name_is_listed_in_the_help_of_the_filter_flag(self) -> None:
        _, text, _ = self.cli("doctor", "--help", expect=EXIT_OK)
        for name in _check_names():
            self.assertIn(name, text)

    def test_no_check_reports_an_unknown_verdict(self) -> None:
        payload = self.doctor_json()
        self.assertTrue(payload["checks"])
        self.assertEqual({check["status"] for check in payload["checks"]} - {OK, WARN, FAIL, SKIPPED}, set())


class ApiProbeCheckTest(DoctorTestCase):
    """The `api` check: skipped unless ``--probe``, and answered from the fixtures when asked.

    This is the one check in the report that spends requests, so the contract is that
    (a) a plain ``fm doctor`` never reaches the network, (b) opting in reports the same
    facts ``fm api probe`` does, and (c) an account that can read nothing warns rather
    than fails.
    """

    FIXTURES = REPO / "tests" / "fixtures" / "api"
    #: The last day the recorded corpus answers for, so a probe's newest question lands
    #: on a fixture instead of on an unrecorded day.
    WHEN = datetime(2026, 5, 14, 12, tzinfo=UTC)
    CATALOG_SCOPES = ALL_SCOPES

    def setUp(self) -> None:
        super().setUp()
        original = os.environ.get("FM_FIXTURES")
        os.environ["FM_FIXTURES"] = str(self.FIXTURES)
        self.addCleanup(
            lambda: os.environ.__setitem__("FM_FIXTURES", original)
            if original is not None
            else os.environ.pop("FM_FIXTURES", None)
        )

    def token_with(self, *scopes: str) -> None:
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        CredentialStore(self.credentials_path).save(
            TokenSet(
                access_token="probe-access-token",
                refresh_token="probe-refresh-token",
                expires_at=datetime.now(UTC) + timedelta(hours=8),
                scopes=scopes,
                user_id="42",
            )
        )

    def api_check(self, *, probe: bool) -> Check:
        return {check.name: check for check in run_checks(self.session(), probe=probe, now=self.WHEN)}["api"]

    def test_it_is_skipped_by_default_so_a_plain_doctor_stays_offline(self) -> None:
        payload = self.doctor_json()
        api = next(check for check in payload["checks"] if check["check"] == "api")
        self.assertEqual(api["status"], SKIPPED)
        self.assertFalse(api["probed"])
        self.assertEqual(api["requests"], 0)
        self.assertIn("fm api probe", api["hint"])
        self.assertEqual(payload["exit_code"], EXIT_OK)

    def test_probing_a_catalog_that_answers_everything_is_ok(self) -> None:
        self.token_with(*self.CATALOG_SCOPES)
        check = self.api_check(probe=True)
        self.assertEqual(check.status, OK, check.detail)
        self.assertTrue(check.detail["probed"])
        self.assertEqual(check.detail["usable"], check.detail["datasets"])
        self.assertEqual(check.detail["blocked"], [])
        self.assertIsNone(check.detail["rate_limit_remaining"], "v4 sends no rate-limit ceiling to report")

    def test_a_missing_scope_warns_and_names_the_scope(self) -> None:
        self.token_with(HEALTH_METRICS_SCOPE, SLEEP_SCOPE)
        check = self.api_check(probe=True)
        self.assertEqual(check.status, WARN)
        # Only a gap that disables a dataset is worth a doctor line: profile.readonly is
        # the identity probe, and its absence is a note in the probe report instead.
        self.assertEqual(sorted(check.detail["missing_scopes"]), ["activity_and_fitness.readonly"])
        self.assertIn("fm api probe", check.hint)
        self.assertNotIn("fail", {check.status})

    def test_nothing_answerable_warns_without_failing_the_report(self) -> None:
        self.token_with("nothing_the_catalog_uses")
        check = self.api_check(probe=True)
        self.assertEqual(check.status, WARN)
        self.assertEqual(check.detail["usable"], 0)
        self.assertEqual(len(check.detail["blocked"]), check.detail["datasets"])

    def test_no_token_is_a_warning_naming_the_fix(self) -> None:
        _code, text, _err = self.cli("doctor", "--check", "api", "--probe", expect=EXIT_OK)
        self.assertIn("[warn]", text)
        self.assertIn("fm auth login", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
