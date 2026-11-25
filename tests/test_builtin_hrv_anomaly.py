"""The built-in ``hrv-anomaly`` plugin: nightly HRV, judged against the wearer's own nights.

This is the second anomaly plugin, and the reason it exists is a shape difference: HRV is
one number per night, so none of the intraday engine's minutes/contexts machinery can
honestly be reused. The tests below are the acceptance criteria that follows from:

* a quiet wearer gets no alerts, and a run that found nothing still says what it judged
  (the bar, the measure, the nights) - a nightly detector that goes silent is indistinguishable
  from one that is broken;
* a run of low nights alerts once, naming both sides of the comparison - the observed ms,
  the wearer's own median and spread, and the bar - because "HRV anomaly" is not actionable;
* a *single* low night does not alert by default, and says which knob would have allowed it;
* high HRV does not alert by default at all, and the option that enables it is opt-in;
* the baseline is provisional early and never claims more than ``critical`` confidence on a
  short history;
* resting HR corroborates and never gates, and "no resting HR" is reported as an absence of
  evidence rather than as evidence of nothing;
* every option is config: the advertised option list *is* the detector's own config class.
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fitbit_monitor.anomaly.hrv import (
    FINDING_HRV_HIGH,
    FINDING_HRV_LOW,
    HrvAnomalyConfig,
    detect_hrv,
)
from fitbit_monitor.config import AppConfig
from fitbit_monitor.plugins.builtin.hrv_anomaly import (
    ENGINE_OPTIONS,
    NON_DIAGNOSTIC_HRV,
    POLICY_OPTIONS,
    HrvAnomalyPlugin,
)
from fitbit_monitor.plugins.builtin.hr_anomaly import NON_DIAGNOSTIC_CODE
from fitbit_monitor.plugins.errors import PluginConfigError
from fitbit_monitor.plugins.models import Severity
from fitbit_monitor.plugins.registry import PluginRecord, PluginRegistry
from fitbit_monitor.plugins.runner import run_plugins
from fitbit_monitor.plugins.sinks import AlertDispatcher, build_dispatcher
from fitbit_monitor.plugins.testing import (
    FIXTURE_END,
    PluginContractTestCase,
    fixture_store,
    make_config,
    run_fixture_plugin,
)
from fitbit_monitor.store.alerts import AlertHistory
from fitbit_monitor.store.downsample import daily_values
from fitbit_monitor.store.reader import ReadOnlyStore
from fitbit_monitor.store.seed import seed_store
from fitbit_monitor.store.writer import SeriesStore
from fitbit_monitor.timeutil import DateRange

#: Same ban the intraday plugin lives under: a description of a signal is not a claim about
#: a condition, and the temptation is strongest in the alert text.
BANNED_CLAIMS = (
    "arrhythmia",
    "atrial",
    "fibrillat",
    "afib",
    "patholog",
    "you have",
    "you are having",
    "see a doctor",
    "call 911",
    "definitely",
)

NIGHT = timedelta(days=1)


def night(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def stored_hrv(path: Path, since: datetime, until: datetime, field: str = "rmssd") -> dict[date, float]:
    with ReadOnlyStore(path) as store:
        return daily_values(store.query("hrv", since, until, fields=[field]), field)


def plant(path: Path, values: dict[date, float], field: str = "rmssd") -> None:
    """Overwrite nightly HRV values - the way a wearer's real nights would have arrived."""
    with SeriesStore(path) as store:
        for day, value in sorted(values.items()):
            store.put("hrv", night(day), {field: round(value, 1)})
        store.commit()


def scale(path: Path, days: set[date], factor: float) -> dict[date, float]:
    """Multiply the stored values on ``days`` by ``factor`` and return what was written."""
    original = stored_hrv(path, night(min(days)) - NIGHT, night(max(days)) + NIGHT)
    planted = {day: original[day] * factor for day in sorted(days) if day in original}
    plant(path, planted)
    return planted


def plant_at_z(path: Path, days: set[date], z: float, span: DateRange | None = None) -> dict[date, float]:
    """Plant ``days`` at ``z`` robust sigmas below the bar that night will be judged against.

    Planting "-30% of whatever was there" is a fragile thing to assert on: whether that is a
    finding depends on the wearer's spread, which the seeded corpus draws from a seed no
    reader can see. So the tests say "4 sigmas down" and let this work out the milliseconds -
    against *each night's own bar*, read from an unjudged pass, because the run's bar and the
    run's median are not the same numbers.
    """
    span = span or window()
    with ReadOnlyStore(path) as store:
        result = detect_hrv(store, span.since, span.until)
    bars = {night.day: (night.level_ms, night.spread_ms) for night in result.nights}
    fallback = (result.baseline.level_ms, result.baseline.spread_ms)
    planted: dict[date, float] = {}
    for day in sorted(days):
        # A night the store does not have yet has no bar of its own; the run's bar is the
        # next best thing, and planting one is exactly what a late import would do.
        level, spread = bars.get(day, fallback)
        planted[day] = level * math.exp(-z * math.log1p(spread / level))
    plant(path, planted)
    return planted


def plant_resting_hr(path: Path, days: set[date], bpm: float) -> None:
    with ReadOnlyStore(path) as reader:
        current = daily_values(
            reader.query("daily_summary", night(min(days)) - NIGHT, night(max(days)) + NIGHT,
                         fields=["resting_hr"]),
            "resting_hr",
        )
    with SeriesStore(path) as store:
        for day in sorted(days):
            store.put(
                "daily_summary",
                night(day),
                {"resting_hr": round(current.get(day, 58.0) + bpm, 1), "steps": 9000.0},
            )
        store.commit()


def fresh_store(directory: str, name: str = "fresh.sqlite3") -> Path:
    """A store with nothing planted in it - the starting point for a "what changed" test."""
    path = Path(directory) / name
    with SeriesStore(path) as store:
        seed_store(store, days=45, seed=20260531, end=FIXTURE_END)
    return path


def window(days: int = 30) -> DateRange:
    """The last ``days`` nights of the fixture corpus."""
    end = FIXTURE_END + timedelta(days=1)
    return DateRange(since=night(end - timedelta(days=days)), until=night(end))


def metrics_of(outcome) -> dict[str, object]:
    return {metric.key: metric for metric in outcome.metrics}


def configured(**values) -> HrvAnomalyPlugin:
    """A plugin configured exactly the way the host configures one."""
    config = make_config(HrvAnomalyPlugin.name, values).plugin_config(
        HrvAnomalyPlugin.name, HrvAnomalyPlugin.declared_defaults()
    )
    plugin = HrvAnomalyPlugin()
    plugin.configure(config)
    return plugin


# --- the shared contract suite ----------------------------------------------
class HrvAnomalyContractTest(PluginContractTestCase):
    """The suite every plugin is held to, including this one."""

    plugin_cls = HrvAnomalyPlugin


# --- the rules ---------------------------------------------------------------
class HrvAnomalyRulesTest(unittest.TestCase):
    """What alerts, what does not, and what is reported instead."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="fm-hrv-anomaly-")
        cls.store_path = fixture_store(cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def setUp(self) -> None:
        # A pristine copy per test: planting changes the corpus, and a later test must not
        # inherit an earlier one's dip.
        self.base = self.store_path
        self.work_dir = tempfile.TemporaryDirectory(prefix="fm-hrv-copy-")
        self.path = Path(self.work_dir.name) / "store.sqlite3"
        with SeriesStore(self.path) as store:
            with ReadOnlyStore(self.base) as reader:
                for dataset in ("hrv", "daily_summary"):
                    for point in reader.query(dataset, night(FIXTURE_END - timedelta(days=60)), night(FIXTURE_END + NIGHT)):
                        store.put(dataset, point.ts, dict(point.values))
                store.commit()
        self.addCleanup(self.work_dir.cleanup)

    def run_plugin(self, **values):
        return run_fixture_plugin(
            HrvAnomalyPlugin, store_path=self.path, window=window(), config_values=values
        )

    # --- quiet is quiet, and says so ---------------------------------------
    def test_a_quiet_corpus_alerts_on_nothing_but_says_what_it_judged(self) -> None:
        outcome = self.run_plugin()
        self.assertEqual([alert.key for alert in outcome.alerts], [], "false positives")
        metrics = metrics_of(outcome)
        baseline = metrics["hrv_anomaly_baseline"]
        self.assertIn("rmssd", str(baseline.value))
        self.assertIn("median", str(baseline.value))
        self.assertTrue(baseline.details["usable"], "the seeded corpus has plenty of nights")
        findings = metrics["hrv_anomaly_findings"]
        self.assertEqual(findings.value, 0)
        self.assertIn("no night", findings.summary)

    # --- a run of nights ----------------------------------------------------
    def test_three_low_nights_alert_once_naming_both_sides(self) -> None:
        days = {FIXTURE_END - timedelta(days=d) for d in (8, 7, 6)}
        planted = plant_at_z(self.path, days, 4.0)
        outcome = self.run_plugin()
        self.assertEqual(len(outcome.alerts), 1, [a.title for a in outcome.alerts])
        alert = outcome.alerts[0]
        self.assertEqual(alert.key, FINDING_HRV_LOW)
        self.assertEqual(alert.details["night_count"], 3)
        self.assertEqual(alert.details["observed_ms"], round(min(planted.values()), 1))
        self.assertGreater(alert.details["baseline_level_ms"], alert.details["observed_ms"])
        self.assertEqual(len(alert.evidence), 3, "one piece of evidence per night")
        self.assertEqual(alert.window_start, night(min(days)))
        self.assertEqual(alert.window_end, night(max(days)) + NIGHT)
        self.assertEqual(alert.dedup_key, f"{FINDING_HRV_LOW}:{min(days).isoformat()}")

    def test_one_low_night_is_held_back_and_names_the_knob(self) -> None:
        day = FIXTURE_END - timedelta(days=7)
        # Below the bar (z_low = 3), short of what a single night needs (deep_dip_z = 4.5).
        plant_at_z(self.path, {day}, 3.5)
        outcome = self.run_plugin()
        self.assertEqual(outcome.alerts, [])
        suppressed = metrics_of(outcome)["hrv_anomaly_suppressed"]
        codes = [item["code"] for item in suppressed.details["candidates"]]
        self.assertIn("night_gate", codes)
        reason = next(item["reason"] for item in suppressed.details["candidates"] if item["code"] == "night_gate")
        self.assertIn("deep_dip_z", reason)
        self.assertIn("min_consecutive_nights", reason)

    def test_one_deep_night_alerts_without_a_second_night(self) -> None:
        day = FIXTURE_END - timedelta(days=7)
        plant_at_z(self.path, {day}, 6.0)
        outcome = self.run_plugin()
        self.assertEqual([a.key for a in outcome.alerts], [FINDING_HRV_LOW])
        self.assertEqual(outcome.alerts[0].details["night_count"], 1)
        self.assertIn("deep_dip_z", outcome.alerts[0].details["because"])

    def test_high_hrv_is_silent_until_it_is_asked_for(self) -> None:
        days = {FIXTURE_END - timedelta(days=d) for d in (9, 8)}
        plant_at_z(self.path, days, -5.0)
        outcome = self.run_plugin()
        self.assertEqual(outcome.alerts, [], "high HRV is not a warning sign")
        metrics = metrics_of(outcome)
        self.assertEqual(metrics["hrv_anomaly_findings"].value, 0)

        loud = self.run_plugin(alert_on_high=True)
        self.assertEqual([a.key for a in loud.alerts], [FINDING_HRV_HIGH])
        self.assertEqual(loud.alerts[0].details["night_count"], 2)

    def test_a_dip_that_eats_the_whole_window_is_still_judged_against_its_start(self) -> None:
        """The point of the two-pass scan: a long dip must not redefine "low" around itself."""
        days = {FIXTURE_END - timedelta(days=d) for d in range(16, 3, -1)}
        plant_at_z(self.path, days, 4.0)
        outcome = self.run_plugin()
        self.assertTrue(outcome.alerts, "a fortnight 4 sigma down is the finding, not the baseline")
        first = min(alert.details["baseline_nights"] for alert in outcome.alerts)
        self.assertGreaterEqual(first, HrvAnomalyConfig.provisional_min_nights)

    def test_a_gap_splits_a_run_rather_than_stepping_over_it(self) -> None:
        days = {FIXTURE_END - timedelta(days=d) for d in (10, 9, 7)}
        # Deep enough that the lone night on its own is still a finding, so the split is
        # visible in the shape of the alerts rather than only in their count.
        plant_at_z(self.path, days, 5.2)
        outcome = self.run_plugin()
        counts = sorted(alert.details["night_count"] for alert in outcome.alerts)
        self.assertEqual(counts[:2], [1, 2], [alert.title for alert in outcome.alerts])

    # --- the baseline -------------------------------------------------------
    def test_a_short_history_is_provisional_and_cannot_reach_critical(self) -> None:
        fresh = Path(self.work_dir.name) / "fresh.sqlite3"
        with SeriesStore(fresh) as store:
            seed_store(store, days=9, seed=99, end=FIXTURE_END)
        days = {FIXTURE_END - timedelta(days=d) for d in (2, 1)}
        original = stored_hrv(fresh, night(FIXTURE_END - timedelta(days=10)), night(FIXTURE_END + NIGHT))
        plant(fresh, {day: original[day] * 0.45 for day in sorted(days) if day in original})
        outcome = run_fixture_plugin(
            HrvAnomalyPlugin,
            store_path=fresh,
            window=DateRange(since=night(FIXTURE_END - timedelta(days=8)), until=night(FIXTURE_END + NIGHT)),
        )
        self.assertTrue(outcome.alerts, "four nights of history is enough to notice -35%")
        for alert in outcome.alerts:
            self.assertIs(alert.details["baseline_established"], False)
            self.assertLessEqual(alert.confidence, HrvAnomalyConfig.provisional_confidence_cap)
            self.assertEqual(alert.severity, Severity.INFO, "a provisional bar never shouts")
            self.assertIn("provisional", alert.message)

    def test_coverage_gates_the_run_and_says_which_knob(self) -> None:
        sparse = Path(self.work_dir.name) / "sparse.sqlite3"
        with SeriesStore(sparse) as store:
            seed_store(store, days=60, seed=7, end=FIXTURE_END)
        # Then wipe most of the window's nights: nine nights left out of thirty.
        keep = {FIXTURE_END - timedelta(days=d) for d in range(9)}
        with SeriesStore(sparse) as store:
            store.delete_range("hrv", night(FIXTURE_END - timedelta(days=40)), night(FIXTURE_END + NIGHT))
            store.commit()
        plant(sparse, {day: 60.0 for day in sorted(keep)})
        outcome = run_fixture_plugin(HrvAnomalyPlugin, store_path=sparse, window=window())
        self.assertEqual(outcome.alerts, [])
        metrics = metrics_of(outcome)
        self.assertIn("hrv_anomaly_declined", metrics)
        self.assertIn("min_window_coverage", str(metrics["hrv_anomaly_declined"].value))
    def test_a_store_that_only_carries_sdnn_says_which_measure_it_judged(self) -> None:
        """`field = "auto"` exists because sources disagree about what HRV even is.

        A Takeout archive carries RMSSD; other pipelines publish SDNN and nothing else. A
        detector that reported nothing because it asked for the column this wearer's device
        never wrote would be the worst kind of quiet, so it takes the column that exists and
        says, in the report, which one it picked and why.
        """
        path = Path(self.work_dir.name) / "sdnn-only.sqlite3"
        dip = {FIXTURE_END - timedelta(days=2), FIXTURE_END - timedelta(days=1)}
        with SeriesStore(path) as store:
            day = FIXTURE_END - timedelta(days=40)
            step = 0
            while day <= FIXTURE_END:
                value = 55.0 + 5.0 * math.sin(step / 3.0)
                store.put("hrv", night(day), {"sdnn": round(25.0 if day in dip else value, 1)})
                day, step = day + NIGHT, step + 1
            store.commit()
        outcome = run_fixture_plugin(HrvAnomalyPlugin, store_path=path, window=window())
        metrics = metrics_of(outcome)
        self.assertTrue(str(metrics["hrv_anomaly_baseline"].value).startswith("sdnn:"))
        self.assertIn("rmssd", str(metrics["hrv_anomaly_baseline"].details["field_note"]))
        self.assertTrue(outcome.alerts, "the two 25 ms nights should not be missed for the column name")
        self.assertEqual(outcome.alerts[0].details["field"], "sdnn")

    def test_a_field_the_store_does_not_carry_says_so_instead_of_being_quiet(self) -> None:
        """Asking for a column nobody wrote must not look the same as a healthy month."""
        outcome = self.run_plugin(field="deep_sleep_rmssd")
        self.assertEqual(outcome.alerts, [])
        metrics = metrics_of(outcome)
        baseline = metrics["hrv_anomaly_baseline"]
        self.assertIn("no values", str(baseline.details["field_note"]))
        notes = metrics["hrv_anomaly_suppressed"].details["rules_that_did_not_run"]
        self.assertTrue(any("nothing to judge" in note["reason"] for note in notes), notes)

    def test_an_empty_window_is_answered_not_ignored(self) -> None:
        empty = Path(self.work_dir.name) / "empty.sqlite3"
        with SeriesStore(empty) as store:
            seed_store(store, days=60, seed=5, end=FIXTURE_END)
            store.delete_range("hrv", night(FIXTURE_END - timedelta(days=70)), night(FIXTURE_END + NIGHT))
            store.commit()
        outcome = run_fixture_plugin(
            HrvAnomalyPlugin,
            store_path=empty,
            window=DateRange(since=night(FIXTURE_END - timedelta(days=20)), until=night(FIXTURE_END + NIGHT)),
        )
        self.assertEqual(outcome.alerts, [])
        metrics = metrics_of(outcome)
        self.assertIn("hrv_anomaly_declined", metrics)
        self.assertIn("nothing was analysed", str(metrics["hrv_anomaly_declined"].value).lower())

    def test_a_malformed_value_is_excluded_and_said(self) -> None:
        """A stored 0 ms is a broken row, not the lowest night of the year."""
        days = {FIXTURE_END - timedelta(days=d) for d in (8, 7)}
        plant(self.path, {day: 0.0 for day in sorted(days)})
        outcome = self.run_plugin()
        self.assertEqual(outcome.alerts, [], "zero is not a finding")
        notes = metrics_of(outcome)["hrv_anomaly_suppressed"].details["rules_that_did_not_run"]
        self.assertTrue(any("non-positive" in note["reason"] for note in notes), notes)

    # --- corroboration ------------------------------------------------------
    def test_resting_hr_moving_the_other_way_adds_confidence_and_says_so(self) -> None:
        days = {FIXTURE_END - timedelta(days=d) for d in (8, 7, 6)}
        plant_at_z(self.path, days, 4.0)
        plain = self.run_plugin()
        lifted = self.run_plugin()
        plant_resting_hr(self.path, days, 9.0)
        corroborated = self.run_plugin()
        self.assertEqual(len(corroborated.alerts), 1)
        note = corroborated.alerts[0].details["corroboration"]
        self.assertTrue(note["corroborated"])
        self.assertIn("not as a cause", note["note"])
        self.assertGreater(
            corroborated.alerts[0].confidence, plain.alerts[0].confidence, "corroboration must show up"
        )
        self.assertFalse(plain.alerts[0].details["corroboration"]["corroborated"])
        self.assertIsNotNone(lifted)

    def test_no_resting_hr_is_an_absence_of_evidence_not_a_contradiction(self) -> None:
        days = {FIXTURE_END - timedelta(days=d) for d in (8, 7, 6)}
        plant_at_z(self.path, days, 4.0)
        with SeriesStore(self.path) as store:
            store.delete_range("daily_summary", night(FIXTURE_END - timedelta(days=45)), night(FIXTURE_END + NIGHT))
            store.commit()
        outcome = self.run_plugin()
        self.assertTrue(outcome.alerts, "still a finding: resting HR is not a gate")
        note = outcome.alerts[0].details["corroboration"]
        self.assertFalse(note["corroborated"])
        self.assertIn("nothing to corroborate with", note["note"])

    def test_a_corroboration_source_that_does_not_exist_is_reported_not_fatal(self) -> None:
        """A typo in `resting_hr_dataset` costs the corroboration, not the run."""
        days = {FIXTURE_END - timedelta(days=d) for d in (8, 7, 6)}
        plant_at_z(self.path, days, 4.0)
        outcome = self.run_plugin(resting_hr_dataset="no_such_dataset")
        self.assertEqual(len(outcome.alerts), 1)
        note = outcome.alerts[0].details["corroboration"]
        self.assertFalse(note["corroborated"])
        self.assertIn("nothing to corroborate with", note["note"])

    def test_corroborate_off_never_opens_the_store_for_resting_hr(self) -> None:
        days = {FIXTURE_END - timedelta(days=d) for d in (8, 7, 6)}
        plant_at_z(self.path, days, 4.0)
        outcome = self.run_plugin(corroborate=False)
        self.assertTrue(outcome.alerts)
        self.assertEqual(outcome.alerts[0].details["corroboration"]["note"][:len("corroborate = false")], "corroborate = false")


# --- options are config ------------------------------------------------------
class HrvAnomalyConfigTest(unittest.TestCase):
    def test_engine_options_are_the_detector_own_list(self) -> None:
        specs = HrvAnomalyConfig.option_specs()
        self.assertEqual([o.name for o in ENGINE_OPTIONS], [name for name, _k, _d, _h in specs])
        self.assertEqual([o.default for o in ENGINE_OPTIONS], [d for _n, _k, d, _h in specs])
        HrvAnomalyConfig.from_mapping({o.name: o.default for o in ENGINE_OPTIONS})
        for option in list(ENGINE_OPTIONS) + list(POLICY_OPTIONS):
            self.assertTrue(option.help, f"{option.name} advertises no help text")

    def test_changing_a_threshold_changes_what_is_found(self) -> None:
        """The knobs are not decoration: each one, moved, moves what the run says."""
        with tempfile.TemporaryDirectory() as tmp:
            days = {FIXTURE_END - timedelta(days=d) for d in (8, 7)}

            pair = fresh_store(tmp, "pair.sqlite3")
            plant_at_z(pair, days, 3.5)
            default = run_fixture_plugin(HrvAnomalyPlugin, store_path=pair, window=window())
            self.assertEqual([a.key for a in default.alerts], [FINDING_HRV_LOW])
            # Widen the sigma bar and the same nights are ordinary again.
            loose = run_fixture_plugin(
                HrvAnomalyPlugin, store_path=pair, window=window(), config_values={"z_low": 8.0}
            )
            self.assertEqual(loose.alerts, [])
            # And a plugin can be told to say nothing this quiet ever reaches it.
            muted = run_fixture_plugin(
                HrvAnomalyPlugin, store_path=pair, window=window(), config_values={"min_confidence": 0.9}
            )
            self.assertEqual(muted.alerts, [])
            held = metrics_of(muted)["hrv_anomaly_suppressed"]
            self.assertIn("low_confidence", [item["code"] for item in held.details["candidates"]])

            single = fresh_store(tmp, "single.sqlite3")
            plant_at_z(single, {FIXTURE_END - timedelta(days=7)}, 3.5)
            quiet = run_fixture_plugin(HrvAnomalyPlugin, store_path=single, window=window())
            self.assertEqual(quiet.alerts, [], "one night is not an alert by default")
            eager = run_fixture_plugin(
                HrvAnomalyPlugin,
                store_path=single,
                window=window(),
                config_values={"min_consecutive_nights": 1},
            )
            self.assertEqual([a.key for a in eager.alerts], [FINDING_HRV_LOW])

    def test_options_coerce_the_shapes_the_host_actually_sends(self) -> None:
        """``--set hrv-anomaly.z_low=4.5`` arrives as a string, and so does ``false``.

        The host coerces declared options before ``configure`` runs, but a value that came
        through a path it does not coerce - a mapping built by hand, a test, another host -
        still has to be understood rather than crash the run with a TypeError.
        """
        plugin = configured(z_low="4.5", min_consecutive_nights="3", corroborate="false", alert_on_high="true")
        self.assertEqual(plugin.anomaly.z_low, 4.5)
        self.assertEqual(plugin.anomaly.min_consecutive_nights, 3)
        self.assertIs(plugin.anomaly.corroborate, False)
        self.assertIs(plugin.anomaly.alert_on_high, True)
        for values, phrase in (
            ({"z_low": "wide"}, "expects a number"),
            ({"min_baseline_nights": "many"}, "expects a whole number"),
            ({"corroborate": "maybe"}, "expects true or false"),
        ):
            with self.subTest(**values):
                with self.assertRaises(PluginConfigError) as caught:
                    configured(**values)
                self.assertIn(phrase, str(caught.exception))

    def test_the_result_serialises_for_the_json_channel(self) -> None:
        """`--json` is a contract: every reported number has to survive a round trip."""
        with tempfile.TemporaryDirectory() as tmp:
            path = fresh_store(tmp, "json.sqlite3")
            plant_at_z(path, {FIXTURE_END - timedelta(days=d) for d in (8, 7)}, 4.0)
            with ReadOnlyStore(path) as store:
                result = detect_hrv(store, window().since, window().until)
            payload = json.loads(json.dumps(result.as_dict()))
            self.assertEqual(payload["field"], result.field)
            self.assertEqual(len(payload["findings"]), 1)
            self.assertIn("because", payload["findings"][0])
            self.assertEqual(payload["baseline"]["field"], result.baseline.field)
            self.assertEqual(len(payload["nights"]), result.nights_seen)
            self.assertTrue(
                all(isinstance(night["z"], float) for night in payload["nights"] if night["z"] is not None)
            )

    def test_every_impossible_detector_option_is_refused_by_name(self) -> None:
        """Each validation message is the only explanation a user gets for a bad ``--set``.

        So each one is tested for the number it quotes and, where a fix exists, the hint:
        a refusal that says "invalid" and nothing else sends the reader back through the
        whole option list.
        """
        cases = (
            ({"baseline_nights": 0}, "positive integer"),
            ({"baseline_nights": 5, "min_baseline_nights": 10}, "cannot exceed"),
            ({"provisional_min_nights": 10}, "must be below"),
            ({"z_low": 0}, "positive number"),
            ({"deep_dip_z": 1.0}, "stricter"),
            ({"min_low_delta_ms": -1}, "non-negative"),
            ({"provisional_confidence_cap": 0.99}, "confidence_ceiling"),
            ({"confidence_ceiling": 1.5}, "confidence_ceiling"),
            ({"field": "bpm"}, "unknown hrv field"),
        )
        for values, phrase in cases:
            with self.subTest(**values):
                with self.assertRaises(PluginConfigError) as caught:
                    configured(**values)
                self.assertIn(phrase, str(caught.exception))

    def test_a_key_this_plugin_never_declared_is_ignored_not_refused(self) -> None:
        """`[plugins.hrv-anomaly] typo = 3` is the runner's note, not a failed run.

        The detector would refuse an unknown key handed straight to it, and that is right for
        a library; the plugin filters to its declared options first, so the message a user
        sees is the runner's one-line "unused config keys" warning for every plugin in the
        run, rather than this plugin refusing to run at all. Same bargain `hr-anomaly` makes.
        """
        plugin = configured(z_low=3.5, typo=7)
        self.assertEqual(plugin.anomaly.z_low, 3.5)
        self.assertNotIn("typo", plugin.anomaly.as_dict())

    def test_a_hint_names_the_fix_when_one_exists(self) -> None:
        """`baseline_nights = 5, min_baseline_nights = 10` has an obvious repair; say it."""
        with self.assertRaises(PluginConfigError) as caught:
            configured(baseline_nights=5, min_baseline_nights=10)
        self.assertIn("raise baseline_nights to at least 10", str(caught.exception.hint))
        with self.assertRaises(PluginConfigError) as caught:
            configured(deep_dip_z=1.0)
        self.assertIn("lower z_low", str(caught.exception.hint))

    def test_an_impossible_policy_is_a_named_config_error(self) -> None:
        cases = (
            ({"warning_confidence": 0.9, "critical_confidence": 0.4}, "must not be above"),
            ({"max_alerts": 0}, "at least 1"),
            ({"cooldown": "not-a-duration"}, "cannot parse duration"),
            ({"cooldown": timedelta(hours=-3)}, "must not be negative"),
            ({"min_consecutive_nights": 0}, "positive integer"),
            ({"provisional_min_nights": 40}, "must be below"),
            ({"min_confidence": 2}, "between 0 and 1"),
        )
        for values, phrase in cases:
            with self.subTest(**values):
                with self.assertRaises(PluginConfigError) as caught:
                    configured(**values)
                self.assertIn(phrase, str(caught.exception))

    def test_a_duration_option_arrives_as_both_a_string_and_a_timedelta(self) -> None:
        for value in ("36h", timedelta(hours=36)):
            self.assertEqual(configured(cooldown=value).cooldown, timedelta(hours=36))


# --- the prose ---------------------------------------------------------------
class HrvAnomalyProseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="fm-hrv-prose-")
        cls.path = fixture_store(cls.tmp.name)
        cls.days = {FIXTURE_END - timedelta(days=d) for d in (8, 7, 6)}
        plant_at_z(cls.path, cls.days, 4.0)
        cls.outcome = run_fixture_plugin(HrvAnomalyPlugin, store_path=cls.path, window=window())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_the_message_names_value_bar_median_spread_and_why(self) -> None:
        alert = self.outcome.alerts[0]
        details = alert.details
        for value in (details["observed_ms"], details["baseline_level_ms"], details["threshold_ms"]):
            self.assertIn(f"{value:.0f}", alert.message, f"{value} is not quoted in the message")
        self.assertIn(str(details["baseline_nights"]), alert.message)
        self.assertIn(details["because"], alert.message)
        self.assertIn("not a diagnosis", alert.message)
        self.assertIn("fm plugins run --name hrv-anomaly", alert.message)
        self.assertEqual(alert.details["framing"], NON_DIAGNOSTIC_HRV)
        self.assertIs(alert.details["non_diagnostic"], True)
        self.assertEqual(alert.labels["framing"], NON_DIAGNOSTIC_CODE)

    def test_the_title_says_hrv_and_the_nights_not_a_condition(self) -> None:
        for alert in self.outcome.alerts:
            self.assertTrue(alert.title.startswith("HRV low:"), alert.title)
            blob = f"{alert.title} {alert.message}".lower()
            for word in BANNED_CLAIMS:
                self.assertNotIn(word, blob)

    def test_the_title_carries_the_number_and_the_dates(self) -> None:
        alert = self.outcome.alerts[0]
        self.assertIn(f"{alert.details['observed_ms']:.0f}", alert.title)
        self.assertIn(min(self.days).isoformat(), alert.title)

    def test_everything_is_json_serialisable(self) -> None:
        """Alerts and metrics are stored as JSON, so nothing in them may be a live object.

        The models freeze their mappings (``MappingProxyType``), which is right for callers
        and invisible to ``json`` only at the top level - hence the ``dict()``.
        """
        for alert in self.outcome.alerts:
            json.loads(json.dumps(dict(alert.details)))
            json.loads(json.dumps(dict(alert.labels)))
        for metric in self.outcome.metrics:
            json.loads(json.dumps(dict(metric.details or {})))

    def test_severity_follows_confidence_and_baseline_maturity(self) -> None:
        """Severity is a volume knob derived from the detector's numbers, never a guess."""
        plugin = configured()
        with ReadOnlyStore(self.path) as store:
            findings = detect_hrv(store, window().since, window().until, config=plugin.anomaly).findings
        for alert in self.outcome.alerts:
            finding = next(f for f in findings if f.dedup_key == alert.dedup_key)
            self.assertEqual(plugin.severity_for(finding), alert.severity)
            self.assertEqual(
                alert.severity,
                Severity.CRITICAL if finding.confidence >= plugin.critical_confidence
                else Severity.WARNING if finding.confidence >= plugin.warning_confidence
                else Severity.INFO,
            )

    def test_a_deep_dip_on_an_established_baseline_reaches_warning(self) -> None:
        deep = Path(self.tmp.name) / "deep.sqlite3"
        with SeriesStore(deep) as store:
            with ReadOnlyStore(self.path) as reader:
                for point in reader.query("hrv", night(FIXTURE_END - timedelta(days=60)), night(FIXTURE_END + NIGHT)):
                    store.put("hrv", point.ts, dict(point.values))
            store.commit()
        plant_at_z(deep, {FIXTURE_END - timedelta(days=6)}, 7.0)
        outcome = run_fixture_plugin(HrvAnomalyPlugin, store_path=deep, window=window())
        self.assertEqual(len(outcome.alerts), 1)
        self.assertIn(outcome.alerts[0].severity, (Severity.WARNING, Severity.CRITICAL))
        self.assertGreaterEqual(outcome.alerts[0].confidence, 0.45)


# --- through the real host and the real sinks --------------------------------
class HrvAnomalySinkTest(unittest.TestCase):
    """The alert has to survive the runner, the history and the cooldown."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="fm-hrv-sink-")
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.store_path = fixture_store(self.tmp.name)
        plant_at_z(self.store_path, {FIXTURE_END - timedelta(days=d) for d in (8, 7, 6)}, 4.0)

    def dispatcher(self) -> AlertDispatcher:
        return build_dispatcher(
            AppConfig._from_raw(None, {"alerts": {"cooldown": "36h"}}),
            store_path=self.store_path,
            data_dir=self.data_dir,
            console=False,
            jsonl=True,
            history=True,
        )

    def run_once(self, dispatcher):
        record = PluginRecord(
            name=HrvAnomalyPlugin.name,
            kind="test",
            location=HrvAnomalyPlugin.__module__,
            plugin_class=HrvAnomalyPlugin,
        )
        store = ReadOnlyStore(self.store_path)
        try:
            report = run_plugins(
                PluginRegistry(records=[record]),
                store,
                window(),
                config=make_config(HrvAnomalyPlugin.name, {}),
                names=[HrvAnomalyPlugin.name],
                dispatcher=dispatcher,
                timeout=60.0,
            )
        finally:
            store.close()
        return report

    def test_alerts_are_stored_delivered_and_then_deduped(self) -> None:
        dispatcher = self.dispatcher()
        first = self.run_once(dispatcher)
        self.assertEqual(len(first.outcomes[0].alerts), 1)
        history = AlertHistory.open(self.store_path)
        stored = history.list()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].plugin, HrvAnomalyPlugin.name)
        written = self.data_dir / "alerts.jsonl"
        self.assertTrue(written.exists() and written.read_text().strip())
        json.loads(written.read_text().strip().splitlines()[-1])

        second = self.run_once(self.dispatcher())
        self.assertEqual(len(second.outcomes[0].alerts), 1, "the plugin re-finds the nights")
        self.assertEqual(len(AlertHistory.open(self.store_path).list()), 1, "the sink must not repeat them")

    def test_a_quiet_run_stores_nothing(self) -> None:
        clean = Path(self.tmp.name) / "clean.sqlite3"
        with SeriesStore(clean) as store:
            seed_store(store, days=45, seed=20260531, end=FIXTURE_END)
        record = PluginRecord(
            name=HrvAnomalyPlugin.name, kind="test", location=HrvAnomalyPlugin.__module__,
            plugin_class=HrvAnomalyPlugin,
        )
        store = ReadOnlyStore(clean)
        try:
            report = run_plugins(
                PluginRegistry(records=[record]), store, window(),
                config=make_config(HrvAnomalyPlugin.name, {}),
                names=[HrvAnomalyPlugin.name],
                dispatcher=self.dispatcher(),
                timeout=60.0,
            )
        finally:
            store.close()
        self.assertEqual(report.outcomes[0].alerts, [])
        self.assertEqual(AlertHistory.open(clean).list(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
