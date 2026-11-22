"""Tests for alert sinks, the dispatcher and the alert history.

The behaviour that matters is delivered exactly once per episode: written to the
history table, appended to JSONL, printed, optionally POSTed - and a repeat inside
the cooldown updates the row instead of notifying again.
"""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fitbit_monitor.errors import ConfigError
from fitbit_monitor.plugins.models import Alert, Evidence, Severity
from fitbit_monitor.plugins.sinks import (
    AlertDispatcher,
    ConsoleSink,
    JsonlSink,
    WebhookSink,
    build_dispatcher,
)
from fitbit_monitor.store.alerts import AlertHistory
from fitbit_monitor.store.writer import SeriesStore

import support


def make_alert(key="hr_high", occurred_at=None, severity="warning", **kw):
    return Alert(
        plugin="hr_anomaly",
        key=key,
        title="Irregular rhythm",
        message="Sustained 128 bpm at rest",
        occurred_at=occurred_at or support.utc(2, 3),
        severity=severity,
        **kw,
    )


class SinksHistoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store_path = self.dir / "store.sqlite3"
        SeriesStore(self.store_path).close()
        self.history = AlertHistory.open(self.store_path)
        self.addCleanup(self.history.close)


class ConsoleSinkTest(unittest.TestCase):
    def test_prints_the_rendered_alert(self):
        lines = []
        self.assertTrue(ConsoleSink(printer=lines.append).emit(make_alert()))
        self.assertIn("Irregular rhythm", lines[0])
        self.assertIn("hr_anomaly", lines[0])


class JsonlSinkTest(SinksHistoryTestCase):
    def test_appends_one_object_per_alert(self):
        path = self.dir / "alerts.jsonl"
        sink = JsonlSink(path)
        self.assertTrue(sink.emit(make_alert()))
        self.assertTrue(sink.emit(make_alert(key="spo2_low", occurred_at=support.utc(2, 4))))
        lines = path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["key"], "hr_high")
        self.assertEqual([a.key for a in sink.read_all()], ["hr_high", "spo2_low"])

    def test_round_trips_evidence_and_severity(self):
        path = self.dir / "alerts.jsonl"
        sink = JsonlSink(path)
        sink.emit(make_alert(severity="critical", evidence=[Evidence(at=support.utc(2), value=128.0)]))
        restored = sink.read_all()[0]
        self.assertEqual(restored.severity, Severity.CRITICAL)
        self.assertEqual(restored.evidence[0].value, 128.0)

    def test_unwritable_path_is_reported_not_raised(self):
        blocked = self.dir / "blocked.jsonl"
        blocked.mkdir()  # a directory where a file must go: the write must fail, not raise
        self.assertFalse(JsonlSink(blocked).emit(make_alert()))

    def test_malformed_line_is_skipped(self):
        path = self.dir / "alerts.jsonl"
        path.write_text('{"broken"\n')
        self.assertEqual(JsonlSink(path).read_all(), [])

    def test_missing_file_reads_empty(self):
        self.assertEqual(JsonlSink(self.dir / "absent.jsonl").read_all(), [])


class WebhookSinkTest(SinksHistoryTestCase):
    class Response:
        def __init__(self, status=204):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_https_is_required(self):
        with self.assertRaises(ConfigError) as ctx:
            WebhookSink("http://example.invalid/hook")
        self.assertIn("https", str(ctx.exception))
        self.assertTrue(WebhookSink("https://example.invalid/hook").url.startswith("https"))

    def test_insecure_override_allows_http_with_a_warning(self):
        import logging

        with self.assertLogs("fitbit_monitor.sinks", level=logging.WARNING) as captured:
            sink = WebhookSink("http://127.0.0.1:9/hook", allow_insecure=True)
        self.assertTrue(sink.emit.__name__ == "emit")
        self.assertTrue(any("plaintext" in line for line in captured.output))

    def test_non_http_scheme_is_refused(self):
        with self.assertRaises(ConfigError):
            WebhookSink("ftp://example.invalid/x", allow_insecure=True)

    def test_posts_only_the_alert_payload(self):
        seen = {}

        def opener(request, timeout=None):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data.decode())
            seen["timeout"] = timeout
            seen["content_type"] = request.get_header("Content-type")
            return self.Response(200)

        sink = WebhookSink("https://example.invalid/hook", opener=opener, timeout=3)
        alert = make_alert(details={"secret_store_path": "/home/me/x.sqlite3"})
        self.assertTrue(sink.emit(alert))
        self.assertEqual(seen["url"], "https://example.invalid/hook")
        self.assertEqual(seen["timeout"], 3)
        self.assertEqual(seen["content_type"], "application/json")
        self.assertEqual(seen["body"]["key"], "hr_high")
        self.assertNotIn("details", seen["body"])

    def test_http_error_status_is_recorded_not_raised(self):
        sink = WebhookSink("https://example.invalid/hook", opener=lambda request, timeout=None: self.Response(500))
        self.assertFalse(sink.emit(make_alert()))
        self.assertEqual(sink.failures, ["HTTP 500"])

    def test_unreachable_endpoint_is_recorded_not_raised(self):
        def opener(request, timeout=None):
            raise OSError("connection refused")

        sink = WebhookSink("https://example.invalid/hook", opener=opener)
        self.assertFalse(sink.emit(make_alert()))
        self.assertIn("connection refused", sink.failures[0])


class DispatcherTest(SinksHistoryTestCase):
    class FailingSink:
        name = "broken"

        def emit(self, alert):
            raise RuntimeError("sink exploded")

    class CountingSink:
        name = "counter"

        def __init__(self):
            self.calls = []

        def emit(self, alert):
            self.calls.append(alert.effective_dedup_key)
            return True

    def dispatcher(self, cooldown=timedelta(hours=36), **kw):
        self.counter = self.CountingSink()
        return AlertDispatcher(
            sinks=[self.counter, self.FailingSink()],
            history=self.history,
            cooldown=cooldown,
            **kw,
        )

    def test_new_alert_is_stored_and_delivered(self):
        delivery = self.dispatcher().dispatch(make_alert())
        self.assertTrue(delivery.is_new)
        self.assertEqual(delivery.delivered_to, ("counter",))
        self.assertEqual(delivery.failed, ("broken",))
        self.assertEqual(self.history.count(), 1)
        self.assertEqual(self.counter.calls, [delivery.alert.effective_dedup_key])

    def test_repeat_within_cooldown_updates_instead_of_notifying(self):
        dispatcher = self.dispatcher()
        first = dispatcher.dispatch(make_alert(occurred_at=support.utc(2, 0)))
        repeat = dispatcher.dispatch(
            make_alert(occurred_at=support.utc(2, 0) + timedelta(minutes=20), confidence=0.9)
        )
        self.assertTrue(first.is_new)
        self.assertFalse(repeat.is_new)
        self.assertEqual(repeat.suppressed_by, "cooldown")
        self.assertEqual(self.counter.calls, [first.alert.effective_dedup_key])
        self.assertEqual(self.history.count(), 1)
        self.assertEqual(self.history.occurrences(first.alert.effective_dedup_key), 2)
        stored = self.history.latest()
        self.assertEqual(stored.last_seen_at, support.utc(2, 0) + timedelta(minutes=20))
        self.assertEqual(stored.occurrences, 2)
        self.assertEqual(dispatcher.suppressed_count, 1)

    def test_repeat_after_the_cooldown_is_a_new_alert(self):
        dispatcher = self.dispatcher(cooldown=timedelta(hours=1))
        dispatcher.dispatch(make_alert(occurred_at=support.utc(2, 0)))
        later = dispatcher.dispatch(make_alert(occurred_at=support.utc(2, 5)))
        self.assertTrue(later.is_new)
        self.assertEqual(self.history.count(), 2)

    def test_different_dedup_keys_do_not_suppress_each_other(self):
        dispatcher = self.dispatcher()
        dispatcher.dispatch(make_alert(key="a"))
        second = dispatcher.dispatch(make_alert(key="b"))
        self.assertTrue(second.is_new)
        self.assertEqual(len(self.counter.calls), 2)

    def test_custom_dedup_key_groups_across_keys(self):
        dispatcher = self.dispatcher()
        dispatcher.dispatch(make_alert(key="a", dedup_key="night-1"))
        second = dispatcher.dispatch(make_alert(key="b", dedup_key="night-1"))
        self.assertFalse(second.is_new)

    def test_dispatcher_without_history_always_delivers(self):
        dispatcher = AlertDispatcher(sinks=[(counter := self.CountingSink())], history=None)
        dispatcher.dispatch(make_alert())
        dispatcher.dispatch(make_alert())
        self.assertEqual(len(counter.calls), 2)

    def test_history_failure_does_not_stop_delivery(self):
        dispatcher = self.dispatcher()
        dispatcher.history = BrokenHistory()
        delivery = dispatcher.dispatch(make_alert())
        self.assertTrue(delivery.is_new)
        self.assertEqual(delivery.delivered_to, ("counter",))

    def test_failures_are_listed_for_the_report(self):
        dispatcher = self.dispatcher()
        dispatcher.dispatch(make_alert())
        self.assertEqual(dispatcher.failures, ["broken: hr_anomaly/hr_high"])

    def test_dispatch_all_returns_one_delivery_per_alert(self):
        dispatcher = self.dispatcher()
        deliveries = dispatcher.dispatch_all([make_alert(), make_alert(occurred_at=support.utc(9))])
        self.assertEqual(len(deliveries), 2)
        self.assertEqual([d.as_dict()["plugin"] for d in deliveries], ["hr_anomaly", "hr_anomaly"])


class BrokenHistory:
    def record(self, alert, cooldown):
        from fitbit_monitor.errors import FmError

        raise FmError("history is on a broken disk")


class AlertHistoryQueryTest(SinksHistoryTestCase):
    def seed(self):
        self.history.record(make_alert(severity="info", occurred_at=support.utc(1)), timedelta(0))
        self.history.record(make_alert(key="spo2_low", severity="critical", occurred_at=support.utc(2)), timedelta(0))
        self.history.record(make_alert(key="other", severity="warning", occurred_at=support.utc(3)), timedelta(0))

    def test_list_is_newest_first_and_filterable(self):
        self.seed()
        self.assertEqual([a.key for a in self.history.list()], ["other", "spo2_low", "hr_high"])
        self.assertEqual([a.key for a in self.history.list(since=support.utc(2))], ["other", "spo2_low"])
        self.assertEqual([a.key for a in self.history.list(severity="critical")], ["spo2_low"])
        self.assertEqual([a.key for a in self.history.list(severity="warning")], ["other", "spo2_low"])
        self.assertEqual([a.key for a in self.history.list(plugin="hr_anomaly", limit=1)], ["other"])
        self.assertEqual(self.history.count(), 3)

    def test_alerts_survive_a_reopen(self):
        self.seed()
        reopened = AlertHistory.open(self.store_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.count(), 3)
        latest = reopened.latest()
        self.assertEqual(latest.key, "other")
        self.assertIsNotNone(latest.alert_id)
        self.assertIsNotNone(latest.first_emitted_at)

    def test_replaying_an_older_range_is_idempotent(self):
        """Re-running over a range that was already processed notifies and counts nothing."""
        first, is_new = self.history.record(make_alert(occurred_at=support.utc(3)), timedelta(hours=36))
        self.assertTrue(is_new)
        replayed, is_new_again = self.history.record(make_alert(occurred_at=support.utc(2)), timedelta(hours=36))
        self.assertFalse(is_new_again)
        self.assertEqual(self.history.count(), 1)
        self.assertEqual(self.history.occurrences(first.effective_dedup_key), 1)
        self.assertEqual(replayed.occurrences, 1)
        exact, is_new_exact = self.history.record(make_alert(occurred_at=support.utc(3)), timedelta(hours=36))
        self.assertFalse(is_new_exact)
        self.assertEqual(exact.alert_id, first.alert_id)

    def test_no_alerts_yet_is_not_an_error(self):
        self.assertIsNone(self.history.latest())
        self.assertIsNone(self.history.latest("absent-key"))
        self.assertEqual(self.history.list(), [])
        self.assertEqual(self.history.count(), 0)

    def test_records_the_window_and_evidence(self):
        alert = make_alert(window_start=support.utc(1), window_end=support.utc(2), evidence=[(support.utc(1), 5.0)])
        stored, is_new = self.history.record(alert, timedelta(0))
        self.assertTrue(is_new)
        self.assertEqual(stored.window_start, support.utc(1))
        self.assertEqual(self.history.latest().evidence[0].value, 5.0)


class BuildDispatcherTest(SinksHistoryTestCase):
    def test_wires_the_configured_sinks(self):
        config = support.make_config(alerts={"jsonl": True, "console": True})
        dispatcher = build_dispatcher(
            config,
            store_path=self.store_path,
            data_dir=self.dir,
            printer=lambda text: None,
        )
        self.addCleanup(dispatcher.history.close)
        self.assertEqual([s.name for s in dispatcher.sinks], ["console", "jsonl"])
        self.assertIsNotNone(dispatcher.history)
        dispatcher.dispatch(make_alert())
        self.assertTrue((self.dir / "alerts.jsonl").exists())

    def test_console_and_jsonl_can_be_turned_off(self):
        config = support.make_config(alerts={"jsonl": False, "console": False})
        dispatcher = build_dispatcher(config, store_path=self.store_path, data_dir=self.dir)
        self.addCleanup(dispatcher.history.close)
        self.assertEqual(dispatcher.sinks, [])

    def test_webhook_is_off_without_a_url_and_on_with_one(self):
        plain = build_dispatcher(support.make_config(alerts={"jsonl": False}), store_path=None, data_dir=self.dir)
        self.assertEqual([s.name for s in plain.sinks], ["console"])
        self.assertIsNone(plain.history)

    def test_configured_webhook_is_wired(self):
        config = support.make_config(alerts={"webhook": {"url": "https://example.invalid/h", "enabled": True}})
        dispatcher = build_dispatcher(config, store_path=None, data_dir=self.dir, opener=lambda r, timeout=None: WebhookSinkTest.Response(200))
        self.assertIn("webhook", [s.name for s in dispatcher.sinks])

    def test_cooldown_comes_from_config(self):
        config = support.make_config(alerts={"cooldown": "90m"})
        dispatcher = build_dispatcher(config, store_path=None, data_dir=self.dir)
        self.assertEqual(dispatcher.cooldown, timedelta(minutes=90))


class RunnerDeliveryTest(SinksHistoryTestCase):
    CountingSink = DispatcherTest.CountingSink

    def test_two_runs_in_one_cooldown_notify_once(self):
        from fitbit_monitor.plugins.registry import PluginRegistry
        from fitbit_monitor.plugins.runner import run_plugins
        from fitbit_monitor.store.reader import ReadOnlyStore

        with SeriesStore(self.store_path) as writable:
            writable.put("daily_summary", support.utc(2), {"resting_hr": 62.0})
            writable.put("daily_summary", support.utc(3), {"resting_hr": 64.0})
        store = ReadOnlyStore(self.store_path)
        self.addCleanup(store.close)
        registry = PluginRegistry._resolve([support.candidate(support.OkPlugin)])
        dispatcher = AlertDispatcher(sinks=[(counter := self.CountingSink())], history=self.history, cooldown=timedelta(hours=36))
        run_plugins(registry, store, support.window(), config=support.make_config(), dispatcher=dispatcher)
        run_plugins(registry, store, support.window(), config=support.make_config(), dispatcher=dispatcher)
        self.assertEqual(len(counter.calls), 1, "the second run must not notify again")
        self.assertEqual(dispatcher.suppressed_count, 1)
        self.assertEqual(self.history.count(), 1)
        self.assertEqual(self.history.occurrences("ok_plugin/resting_hr_high/warning"), 2)


if __name__ == "__main__":
    unittest.main()
