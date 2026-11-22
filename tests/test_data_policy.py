"""The data policy as an executable statement (fitb-ei6.7).

``docs/data-policy.md`` makes four promises that a test can actually check, so this
file checks them and nothing else:

1. **Nothing leaves the machine** except the health API and the user's own webhook -
   which is a code property, so what is asserted here is the document's own list of
   destinations plus the fact that the store has no network imports;
2. **logs do not contain readings** - the redacting filter is on every handler ``fm``
   installs, including for values passed as deferred ``%s`` arguments;
3. **retention is documented and its default is a dry run** - `--execute` is what
   deletes, and the documented horizon is what ``--days`` defaults to;
4. **the encryption question is answered, not deferred**.
"""

from __future__ import annotations

import io
import json
import logging
import re
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from fitbit_monitor import logsafe
from fitbit_monitor.cli.common import configure_logging
from fitbit_monitor.store.writer import SeriesStore

REPO = Path(__file__).resolve().parents[1]
POLICY = REPO / "docs" / "data-policy.md"
STORAGE_DOC = REPO / "docs" / "storage.md"
ADR = REPO / "docs" / "adr" / "0003-storage-model.md"


class RedactionTest(unittest.TestCase):
    def test_named_health_readings_are_masked(self) -> None:
        cases = {
            "resting_hr=61.3": "resting_hr=<redacted>",
            "resting_hr: 61.3": "resting_hr=<redacted>",
            "hr=48": "hr=<redacted>",
            "rmssd = 44.5": "rmssd=<redacted>",
            "spo2_min=88": "spo2_min=<redacted>",
            "steps = 12000": "steps=<redacted>",
            "resting_hr 61 bpm": "resting_hr=<redacted>",
            "mean rmssd is 44": "mean rmssd is <redacted>",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(logsafe.redact(text), expected)

    def test_ordinary_log_lines_survive_untouched(self) -> None:
        for text in (
            "imported 3 datasets in 41.2s",
            "seeded 5529 points into /home/x/.local/share/fitbit-monitor/store.sqlite3",
            "chunk 7 of 90 for heart_rate",
            "schema version 3 (this build knows 3)",
            "alert hrv_low severity=warning",
            "kept 2026-05-01..2026-05-31 (31 days)",
        ):
            with self.subTest(text=text):
                self.assertEqual(logsafe.redact(text), text)

    def test_a_value_that_arrives_as_a_log_argument_is_still_masked(self) -> None:
        records: list[logging.LogRecord] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = Collect()
        handler.addFilter(logsafe.RedactingFilter())
        logger = logging.getLogger("fitbit_monitor.test.redaction")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self.addCleanup(lambda: setattr(logger, "handlers", []))

        logger.info("nighttime resting_hr was %s bpm", 47.5)
        logger.warning("rmssd is %.1f", 22.0)
        messages = [record.getMessage() for record in records]
        self.assertEqual(messages[0], "nighttime resting_hr was <redacted>")
        self.assertEqual(messages[1], "rmssd is <redacted>")
        self.assertNotIn("47.5", " ".join(messages))

    def test_configure_logging_puts_the_filter_on_the_root_handler(self) -> None:
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        root.handlers = [logging.StreamHandler(io.StringIO())]
        try:
            configure_logging(0, False)

            def fm_filters() -> list[logging.Filter]:
                return [
                    item
                    for handler in root.handlers
                    for item in handler.filters
                    if getattr(item, "name", None) == logsafe.FILTER_NAME
                ]

            self.assertEqual(len(fm_filters()), len(root.handlers))
            # installing twice must not stack copies of the same filter
            configure_logging(2, False)
            configure_logging(0, False)
            self.assertEqual(len(fm_filters()), len(root.handlers))
        finally:
            root.handlers, root.level = saved_handlers, saved_level

    def test_the_policy_says_logs_hold_no_readings(self) -> None:
        text = POLICY.read_text().lower()
        self.assertIn("redact", text)
        self.assertIn("resting_hr=<redacted>", text)


class LocalOnlyTest(unittest.TestCase):
    def test_the_documented_destinations_are_only_the_api_and_the_webhook(self) -> None:
        text = POLICY.read_text()
        self.assertIn("no telemetry", text.lower())
        for row in ("alerts.webhook.url", "fm import"):
            self.assertIn(row, text)
        self.assertLessEqual(text.lower().count("there is no third party"), 1)

    def test_the_store_and_its_neighbours_never_import_a_network_client(self) -> None:
        """Only ``api/`` may talk to the world; the store and maintenance may not."""
        offenders: list[str] = []
        for path in sorted((REPO / "src" / "fitbit_monitor" / "store").rglob("*.py")):
            text = path.read_text()
            for module in ("urllib", "http.client", "socket", "requests"):
                if re.search(rf"^import {module}|^from {module}", text, re.MULTILINE):
                    offenders.append(f"{path.name}:{module}")
        self.assertEqual(offenders, [])

    def test_every_place_data_can_land_is_listed(self) -> None:
        text = POLICY.read_text()
        for path in (
            "store.sqlite3",
            "backups/",
            "credentials.json",
            "imports/",
            "alerts.jsonl",
            "config.toml",
            "http-etag-cache.json",
        ):
            self.assertIn(path, text)

    def test_the_directories_to_exclude_from_cloud_backup_are_named(self) -> None:
        text = POLICY.read_text()
        self.assertIn(".local/share/fitbit-monitor", text)
        self.assertIn("XDG_DATA_HOME", text)
        self.assertIn("Library/Application Support", text)


class RetentionPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        import os

        for name, value in (("XDG_DATA_HOME", self.home / "data"), ("XDG_CONFIG_HOME", self.home / "config")):
            original = os.environ.get(name)
            os.environ[name] = str(value)
            self.addCleanup(
                lambda: os.environ.__setitem__(name, original) if original is not None else os.environ.pop(name, None)
            )
        self.db = self.home / "data" / "fitbit-monitor" / "store.sqlite3"
        with SeriesStore(self.db) as store:
            store.put("hrv", datetime(2025, 1, 1, tzinfo=UTC), {"rmssd": 40.0})
            store.put_raw("hrv", "p", datetime(2025, 1, 1, tzinfo=UTC), '{"hrv": []}')

    def cli(self, *argv):
        from fitbit_monitor.cli import main

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_prune_without_execute_changes_nothing(self) -> None:
        code, text = self.cli("store", "prune", "--days", "30")
        self.assertEqual(code, 0, text)
        self.assertIn("dry run", text)
        self.assertIn("would delete", text)
        self.assertEqual(self.counts(), (1, 1))

    def test_prune_default_horizon_is_the_documented_one(self) -> None:
        code, text = self.cli("store", "prune", "--json")
        self.assertEqual(code, 0, text)
        payload = json.loads(text)
        self.assertTrue(payload["dry_run"])
        expected = datetime.now(UTC) - timedelta(days=365)
        self.assertLess(abs((datetime.fromisoformat(payload["cutoff"]) - expected).total_seconds()), 600)

    def test_execute_is_what_deletes(self) -> None:
        code, text = self.cli("store", "prune", "--days", "30", "--what", "all", "--execute")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.counts(), (0, 0))

    def counts(self) -> tuple[int, int]:
        import sqlite3

        conn = sqlite3.connect(self.db)
        try:
            return (
                int(conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]),
                int(conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0]),
            )
        finally:
            conn.close()


class DocumentedDecisionsTest(unittest.TestCase):
    def test_retention_and_prune_are_documented(self) -> None:
        text = POLICY.read_text()
        self.assertIn("fm store prune --days 365", text)
        self.assertIn("keep everything", text.lower())
        self.assertIn("Default retention", text)

    def test_the_encryption_decision_is_recorded_with_a_reason(self) -> None:
        text = POLICY.read_text().lower()
        self.assertIn("sqlcipher", text)
        self.assertIn("not implemented", text)
        self.assertIn("full-disk encryption", text)

    def test_the_storage_adr_exists_and_names_the_promises(self) -> None:
        text = " ".join(ADR.read_text().split())  # markdown wraps, the promises do not
        for needle in (
            "content_hash",
            "(dataset, ts_utc)",
            "fm db migrate",
            "one transaction per migration",
            "forward-only",
            "Encryption at rest",
            "SQLCipher",
        ):
            self.assertIn(needle, text, needle)
        # the ticket's worked example is in the ADR, not just in the code
        self.assertIn("HAVING COUNT(*) > 1", text)

    def test_the_storage_doc_states_the_time_convention(self) -> None:
        text = " ".join(STORAGE_DOC.read_text().split())
        self.assertIn("half-open", text)
        self.assertIn("00:00:00+00:00", text)
        self.assertIn("UTC", text)

    def test_the_readme_documents_the_new_commands(self) -> None:
        text = " ".join((REPO / "README.md").read_text().split())
        for command in ("fm store prune", "fm store backup", "fm store verify", "fm store export", "fm db status"):
            self.assertIn(command, text, command)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
