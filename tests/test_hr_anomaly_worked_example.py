"""The worked example, end to end: seeded store -> engine -> plugin -> sinks -> history -> CLI.

The four tickets under this epic each have their own suite - the feature library, the
engine, the plugin, the labelled accuracy gate - and each of those suites tests its own
layer against its own fixtures. What none of them did was walk the chain a user walks, in
one pass, through the real command line. That gap is not hypothetical: the sink layer and
`fm plugins run --json` were both green while the documented pipeline

    fm plugins run --name hr-anomaly --since 20d --json | python -m json.tool

failed with a JSONDecodeError for any user who actually had something to alert on,
because the console sink printed its human-readable block to stdout ahead of the JSON
document. `test_json_stdout_is_exactly_one_json_document_when_alerts_fire` below is that
bug, pinned at the seam rather than in either half.

Everything in here runs `main(argv)` in-process against a seeded store in a temp dir, the
same way `test_cli.py` does, and asserts on what a person would see: exit codes, the
metrics on stdout, the alerts in the history store and the JSONL file, the config keys in
`config.toml`, and the accuracy gate's exit code. Nothing here reaches into the engine's
internals; if a layer's own suite is honest, this file only has to catch the seams.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fitbit_monitor.cli import main
from fitbit_monitor.paths import ALERT_LOG_FILENAME
from fitbit_monitor.plugins.builtin.hr_anomaly import NON_DIAGNOSTIC_CODE

# One list, not two: this is the same ban the plugin's own contract test enforces at the
# message-building layer, applied here to whatever a real run actually delivered.
from test_builtin_hr_anomaly import BANNED_CLAIMS

#: A seeded corpus, a fixed end date and an explicit window, so every number below is
#: reproducible without depending on today's date.
SEED = 1337
DAYS = 45
END = date(2026, 5, 31)
SINCE = "2026-04-01"
UNTIL = "2026-05-31"

#: The metric keys the reference example promises in docs/plugins.md.
PROMISED_METRICS = (
    "hr_anomaly_data_coverage",
    "hr_anomaly_baseline",
    "hr_anomaly_findings",
    "hr_anomaly_alerts",
    "hr_anomaly_suppressed",
)


class WorkedExampleTest(unittest.TestCase):
    """One seeded store per test, one CLI per assertion, no mocks anywhere."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.data_dir = self.home / "data"
        self.config_path = self.home / "config-home" / "fitbit-monitor" / "config.toml"
        self.alert_log = self.data_dir / "fitbit-monitor" / ALERT_LOG_FILENAME
        self._setenv("XDG_DATA_HOME", str(self.data_dir))
        self._setenv("XDG_CONFIG_HOME", str(self.home / "config-home"))
        self._setenv("HOME", str(self.home))
        self.seed()

    # --- the harness ---------------------------------------------------------
    def _setenv(self, name: str, value: str) -> None:
        original = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(
            lambda n=name, o=original: (
                os.environ.__setitem__(n, o) if o is not None else os.environ.pop(n, None)
            )
        )

    def fm(self, *argv: str, expect: int | None = 0) -> tuple[int, str, str]:
        """Run the CLI in-process; return (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        if expect is not None:
            self.assertEqual(code, expect, f"fm {' '.join(argv)} ->\n{out.getvalue()}{err.getvalue()}")
        return code, out.getvalue(), err.getvalue()

    def seed(self) -> None:
        self.fm("dev", "seed", "--days", str(DAYS), "--end", END.isoformat(), "--seed", str(SEED))

    def write_config(self, text: str) -> None:
        """Write the file the CLI reads for `[plugins.hr-anomaly]`."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(text, encoding="utf-8")

    def run_json(self, *extra: str, deliver: bool = True) -> dict[str, Any]:
        """``fm plugins run --name hr-anomaly ... --json``, parsed. Fails if stdout is not JSON."""
        args = [
            "plugins",
            "run",
            "--name",
            "hr-anomaly",
            "--since",
            SINCE,
            "--until",
            UNTIL,
            "--json",
        ]
        if not deliver:
            args.append("--no-deliver")
        code, out, err = self.fm(*args, *extra, expect=None)
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:  # pragma: no cover - the failure IS the point
            self.fail(f"stdout was not one JSON document ({exc}):\n{out[:400]}\n--- stderr ---\n{err[:400]}")
        self.assertEqual(code, payload["exit_code"], "the exit code and the document must agree")
        return payload

    @staticmethod
    def outcome(payload: dict[str, Any]) -> dict[str, Any]:
        outcomes = payload["outcomes"]
        assert len(outcomes) == 1, outcomes
        return outcomes[0]

    @staticmethod
    def metrics(outcome: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {m["key"]: m for m in outcome["metrics"]}

    @staticmethod
    def all_metrics(outcome: dict[str, Any], key: str) -> list[dict[str, Any]]:
        """Every metric row with this key - `hr_anomaly_baseline` has one per context."""
        return [m for m in outcome["metrics"] if m["key"] == key]

    def history(self) -> list[dict[str, Any]]:
        _, out, _ = self.fm("alerts", "list", "--plugin", "hr-anomaly", "--json")
        return json.loads(out)["alerts"]

    # --- "reads stored heart rate ... and raises an alert" -------------------
    def test_a_run_over_stored_data_finds_the_injected_episodes(self) -> None:
        outcome = self.outcome(self.run_json(deliver=False))
        self.assertEqual(outcome["status"], "ok")
        self.assertGreaterEqual(len(outcome["alerts"]), 1, "the seeded corpus has labelled episodes to find")
        metrics = self.metrics(outcome)
        for key in PROMISED_METRICS:
            self.assertIn(key, metrics, "docs/plugins.md promises these metrics on every run")
        # A finding is a measurement with a confidence on it, not a yes/no.
        for alert in outcome["alerts"]:
            self.assertGreater(alert["confidence"], 0.0)
            self.assertLessEqual(alert["confidence"], 1.0)
            self.assertTrue(alert["dedup_key"])
            self.assertGreaterEqual(len(alert["evidence"]), 1, "an alert without evidence cannot be checked")

    def test_the_baseline_is_the_wearers_own_and_says_what_it_is_built_from(self) -> None:
        outcome = self.outcome(self.run_json(deliver=False))
        baselines = self.all_metrics(outcome, "hr_anomaly_baseline")
        self.assertTrue(baselines, "a per-user baseline is the whole premise")
        self.assertGreaterEqual(len(baselines), 2, "one per context, so the metric is not a single global average")
        states = {m["labels"]["state"] for m in baselines}
        self.assertTrue(states <= {"established", "provisional", "refused"})
        self.assertEqual(states, {"established"}, "45 seeded days is past min_baseline_days")
        self.assertEqual({m["labels"]["context"] for m in baselines}, {"rest", "sleep"})
        for m in baselines:
            self.assertGreater(m["value"], 30.0)
            self.assertLess(m["value"], 120.0)
            self.assertGreaterEqual(m["summary"].count("established over"), 1, "it says what it is built from")

    # --- "... and raises an alert" through the standard sinks ----------------
    def test_alerts_reach_history_and_the_jsonl_file_exactly_once(self) -> None:
        outcome = self.outcome(self.run_json())
        raised = len(outcome["alerts"])
        self.assertGreaterEqual(raised, 1)
        self.assertTrue(self.alert_log.exists(), f"the jsonl sink should have written {self.alert_log}")
        self.assertEqual(len(self.alert_log.read_text(encoding="utf-8").splitlines()), raised)
        rows = self.history()
        self.assertEqual(len(rows), raised)
        self.assertEqual({r["plugin"] for r in rows}, {"hr-anomaly"})
        self.assertTrue(all(r["alert_id"] for r in rows), "a stored alert has an id")

    def test_running_the_same_window_twice_does_not_alert_twice(self) -> None:
        """The sink's cross-run cooldown, at the CLI: the second scan is a no-op."""
        first = self.outcome(self.run_json())
        self.assertGreaterEqual(len(first["alerts"]), 1)
        second = self.outcome(self.run_json())
        self.assertEqual(
            [d["suppressed_by"] for d in second["deliveries"]],
            ["cooldown"] * len(second["deliveries"]),
            "a repeat scan of the same window must not re-notify",
        )
        for delivery in second["deliveries"]:
            self.assertEqual(delivery["delivered_to"], [], "nothing may be written to a sink twice")
        self.assertEqual(len(self.history()), len(first["alerts"]))
        self.assertEqual(len(self.alert_log.read_text(encoding="utf-8").splitlines()), len(first["alerts"]))

    def test_no_deliver_computes_everything_and_writes_nothing(self) -> None:
        outcome = self.outcome(self.run_json(deliver=False))
        self.assertEqual(outcome["deliveries"], [])
        self.assertGreaterEqual(len(outcome["alerts"]), 1, "--no-deliver still shows what would fire")
        self.assertFalse(self.alert_log.exists())
        self.assertEqual(self.history(), [])

    # --- the stdout contract that the bug above broke ------------------------
    def test_json_stdout_is_exactly_one_json_document_when_alerts_fire(self) -> None:
        """docs/plugins.md pipes this straight into `python -m json.tool`."""
        code, out, err = self.fm(
            "plugins", "run", "--name", "hr-anomaly", "--since", SINCE, "--until", UNTIL, "--json"
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)  # raises, and fails the test, if anything else is on stdout
        self.assertEqual(payload["counts"]["ok"], 1)
        self.assertGreaterEqual(len(self.outcome(payload)["alerts"]), 1)
        # The alert still reached a human: the console block went to stderr instead.
        self.assertIn("[CRITICAL]", err)
        self.assertTrue(out.lstrip().startswith("{"), "stdout must begin with the JSON document")
        self.assertNotIn("[CRITICAL]", out)

    def test_the_documented_pipeline_parses(self) -> None:
        """docs/plugins.md pipes `--json` straight into `python -m json.tool`.

        Bounded with `--until` rather than the docs' open-ended `--since`, because a
        window that ends today would hold no seeded data at all and this would pass
        without ever alerting - which is exactly how this stayed broken.
        """
        _, out, _ = self.fm("plugins", "run", "--name", "hr-anomaly", "--since", SINCE, "--until", UNTIL, "--json")
        payload = json.loads(out)
        self.assertGreaterEqual(
            len(self.outcome(payload)["alerts"]), 1, "the pipeline has to be proven on a run with alerts"
        )

    # --- honest framing, on the alerts a real run produced -------------------
    def test_every_alert_says_what_it_is_not(self) -> None:
        for alert in self.outcome(self.run_json(deliver=False))["alerts"]:
            message = alert["message"]
            self.assertIn("not a diagnosis", message)
            self.assertIn("not medical advice", message)
            self.assertIn("wrist-worn consumer sensor", message)
            self.assertEqual(alert["labels"]["framing"], NON_DIAGNOSTIC_CODE)
            self.assertIs(alert["details"]["non_diagnostic"], True)
            # ...and names how to check it, with a command that really runs.
            self.assertIn("fm store export --dataset heart_rate --since", message)
            self.assertIn("fm alerts list --plugin hr-anomaly", message)

    def test_no_alert_names_a_condition(self) -> None:
        for alert in self.outcome(self.run_json(deliver=False))["alerts"]:
            blob = f"{alert['title']} {alert['message']}".lower()
            for word in BANNED_CLAIMS:
                self.assertNotIn(word, blob, f"{alert['key']}: {word!r} is a diagnosis, not a measurement")

    def test_the_command_an_alert_points_at_actually_prints_the_readings(self) -> None:
        """An alert that says "check it yourself" owes the user a command that works.

        The inspect command is built from the finding's own window, so running it is also
        the check that the window in the prose is the window the data supports.
        """
        for alert in self.outcome(self.run_json(deliver=False))["alerts"]:
            command = alert["details"]["inspect"]
            self.assertTrue(command.startswith("fm store export"), command)
            argv = shlex.split(command)[1:]  # main() is the program; argv is what follows it
            since = datetime.fromisoformat(argv[argv.index("--since") + 1])
            until = datetime.fromisoformat(argv[argv.index("--until") + 1])
            _, out, err = self.fm(*argv)
            rows = [line.split(",") for line in out.splitlines() if line.startswith("heart_rate,")]
            self.assertTrue(rows, f"{command} printed no readings:\n{out}{err}")
            stamps = [datetime.fromisoformat(row[1]) for row in rows]
            self.assertTrue(all(since <= ts < until for ts in stamps), "the export fell outside the alert's window")
            self.assertGreaterEqual(len(stamps), alert["details"]["readings"], "fewer readings than the alert counted")

    # --- suppression is visible, and config reaches the policy ---------------
    def test_suppression_is_reported_with_the_reason_and_who_decided(self) -> None:
        metric = self.metrics(self.outcome(self.run_json(deliver=False)))["hr_anomaly_suppressed"]
        details = metric["details"]
        suppressed = int(metric["value"])
        self.assertGreater(suppressed, 0, "the seeded corpus has episodes that must not alert")
        self.assertEqual(sum(details["by_code"].values()), suppressed)
        self.assertEqual(sum(details["by_source"].values()), suppressed)
        self.assertEqual(details["listed"], suppressed, "counting without listing is deniable")
        self.assertEqual(len(details["episodes"]), suppressed)
        for episode in details["episodes"]:
            self.assertIn(episode["decided_by"], {"engine", "hr-anomaly"})
            self.assertTrue(episode["reason"], "a suppression has to say why")

    def test_a_policy_floor_set_in_config_suppresses_and_says_so(self) -> None:
        """Per-plugin config from config.toml reaches the alerting policy."""
        self.write_config('[plugins.hr-anomaly]\nmin_confidence = 0.99\n')
        metrics = self.metrics(self.outcome(self.run_json(deliver=False)))
        self.assertGreater(metrics["hr_anomaly_findings"]["value"], 0, "findings still exist at 0.99")
        self.assertEqual(metrics["hr_anomaly_alerts"]["value"], 0.0)
        by = metrics["hr_anomaly_suppressed"]["details"]
        self.assertGreaterEqual(by["by_code"].get("low_confidence", 0), 1)
        self.assertEqual(by["by_source"].get("hr-anomaly"), by["by_code"]["low_confidence"])

    def test_a_window_too_thin_to_mean_anything_declines_in_text_and_json(self) -> None:
        self.write_config('[plugins.hr-anomaly]\nmin_window_coverage = 0.99\n')
        payload = self.run_json(deliver=False)
        outcome = self.outcome(payload)
        metrics = self.metrics(outcome)
        self.assertEqual(metrics["hr_anomaly_alerts"]["value"], 0.0)
        declined = metrics["hr_anomaly_declined"]
        self.assertIn("min_window_coverage", declined["summary"] + str(declined["value"]))
        _, text, _ = self.fm("plugins", "run", "--name", "hr-anomaly", "--since", SINCE, "--until", UNTIL, "--no-deliver")
        self.assertIn("nothing was alerted on", text, "the text report has to say it too")

    def test_a_disabled_plugin_is_skipped_rather_than_silently_absent(self) -> None:
        self.write_config('[plugins.hr-anomaly]\nenabled = false\n')
        payload = self.run_json(deliver=False)
        self.assertEqual(payload["counts"]["skipped"], 1)
        self.assertEqual(self.outcome(payload)["status"], "skipped")

    def test_a_key_nobody_reads_is_named_not_swallowed(self) -> None:
        self.write_config('[plugins.hr-anomaly]\nz_hih = 3.0\n')
        outcome = self.outcome(self.run_json(deliver=False))
        self.assertIn("z_hih", json.dumps(outcome["unknown_config_keys"]))

    # --- the stated accuracy gate, from the same command surface -------------
    def test_the_accuracy_gate_passes_when_asked_from_the_cli(self) -> None:
        """`fm dev accuracy` is the shipped gate; one wearer keeps this test cheap."""
        code, out, err = self.fm("dev", "accuracy", "--wearers", "1", expect=None)
        self.assertEqual(code, 0, f"a floor was missed:\n{out}{err}")
        self.assertIn("precision", out)
        self.assertNotIn("FAIL", out)
        _, payload_text, _ = self.fm("dev", "accuracy", "--wearers", "1", "--json")
        self.assertEqual(json.loads(payload_text)["floors_failed"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
