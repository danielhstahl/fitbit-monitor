"""Configuration: every threshold reachable from a file or an env var, and none of them silent.

The ticket's acceptance line is "thresholds are data-driven config". That means four things,
each tested here: the numbers are *fields* with stated defaults; they can be *overridden* from
the same places every other option in this project is overridden, with the same coercion;
*invalid* combinations are refused rather than quietly clamped; and the surface a plugin
advertises is *derived* from the dataclass, so the two cannot drift apart.
"""

from __future__ import annotations

import json

from dataclasses import fields

from fitbit_monitor.anomaly import AnomalyConfig, declared_option_names
from fitbit_monitor.anomaly.errors import AnomalyConfigError
import unittest


class DeclaredSurfaceTestCase(unittest.TestCase):
    """Nothing the engine uses may be invisible to the person who has to configure it."""

    def test_every_field_is_declared_as_an_option(self) -> None:
        # The point of the whole module: a threshold the engine can use is a threshold someone
        # can set, and a plugin's option list is generated from this rather than typed out by
        # hand, so the two cannot drift apart.
        names = {field.name for field in fields(AnomalyConfig)}
        self.assertEqual(names, set(declared_option_names()), "a config field with no option spec")

    def test_option_specs_are_rendered_for_a_plugin_contract(self) -> None:
        specs = AnomalyConfig.option_specs()
        defaults = AnomalyConfig()
        for name, kind, default, help_text in specs:
            self.assertIn(kind, ("int", "float", "bool", "str", "list"))
            self.assertTrue(help_text, f"{name} has no help text")
            actual = getattr(defaults, name)
            if name == "time_zone":
                actual = actual or "UTC"  # unset means UTC, and the advertised default says so
            if isinstance(actual, tuple):
                actual = list(actual)
            self.assertEqual(actual, default, f"{name}: advertised default is not the shipped one")
            self.assertEqual(type(actual), type(default), name)

    def test_the_numbers_a_rule_divides_by_are_all_reachable(self) -> None:
        # A shortlist of the knobs the docs name, asserted by presence rather than by count: if
        # one of these ever stops being configurable the docs are lying.
        wanted = {
            "z_high",
            "z_low",
            "min_sustained_minutes",
            "smoothing_minutes",
            "merge_gap_minutes",
            "irregularity_block_minutes",
            "irregularity_z",
            "irregularity_floor",
            "min_spread_bpm",
            "min_coverage",
            "confidence_ceiling",
            "provisional_confidence_cap",
            "nocturnal_low_margin_bpm",
            "context_warmup_hours",
        }
        self.assertTrue(
            wanted <= set(declared_option_names()), wanted - set(declared_option_names())
        )


class OverrideTestCase(unittest.TestCase):
    """The config file's strings are the same knobs as the code's numbers."""

    def test_strings_are_coerced_the_way_a_config_file_coerces_them(self) -> None:
        config = AnomalyConfig.from_mapping(
            {
                "z_high": "5.5",
                "min_sustained_minutes": "45",
                "contexts": "sleep",
                "exclude_exercise_in": ["rest", "sleep"],
                "irregularity_floor": 0.2,
            }
        )
        self.assertEqual(config.z_high, 5.5)
        self.assertEqual(config.min_sustained_minutes, 45.0)
        self.assertEqual(config.contexts, ("sleep",))
        self.assertEqual(config.exclude_exercise_in, ("rest", "sleep"))
        self.assertEqual(config.irregularity_floor, 0.2)

    def test_an_unknown_option_is_refused_not_ignored(self) -> None:
        with self.assertRaises(AnomalyConfigError) as caught:
            AnomalyConfig.from_mapping({"min_baselines_days": 9})
        self.assertIn("unknown detection option", str(caught.exception).lower())
        self.assertIsNotNone(caught.exception.hint)

    def test_replacing_a_value_revalidates(self) -> None:
        config = AnomalyConfig()
        self.assertEqual(config.replace(z_high=2.0).z_high, 2.0)
        with self.assertRaises(AnomalyConfigError):
            config.replace(z_high=0.5)

    def test_minutes_are_also_exposed_as_durations(self) -> None:
        self.assertEqual(AnomalyConfig().duration("min_sustained_minutes").total_seconds(), 1200.0)

    def test_as_dict_is_json_and_round_trips_back_into_a_config(self) -> None:
        config = AnomalyConfig(z_high=6.0, contexts=("sleep",))
        document = json.loads(json.dumps(config.as_dict()))
        self.assertEqual(document["z_high"], 6.0)
        self.assertEqual(document["contexts"], ["sleep"])
        self.assertEqual(AnomalyConfig.from_mapping(document).z_high, 6.0)

    def test_an_unset_timezone_stays_unset_rather_than_becoming_a_zone_called_none(self) -> None:
        self.assertIsNone(AnomalyConfig.from_mapping({"time_zone": None}).time_zone)
        self.assertEqual(AnomalyConfig.from_mapping({"time_zone": ""}).time_zone, None)
        self.assertEqual(AnomalyConfig.from_mapping({"time_zone": "UTC"}).time_zone, "UTC")
        with self.assertRaises(AnomalyConfigError):
            AnomalyConfig.from_mapping({"time_zone": "Mars/Olympus_Mons"})


class ValidationTestCase(unittest.TestCase):
    """The refusals. Each one is a combination that would otherwise produce silent nonsense."""

    def check(self, **changes) -> None:
        with self.assertRaises(AnomalyConfigError, msg=f"{changes} should have been refused"):
            AnomalyConfig(**changes)

    def test_a_z_score_below_one_is_inside_everyones_spread(self) -> None:
        self.check(z_high=0.9)
        self.check(z_low=0.5)
        self.check(irregularity_z=0.25)

    def test_provisional_cannot_be_stricter_than_established(self) -> None:
        self.check(provisional_min_days=9, min_baseline_days=7)

    def test_the_baseline_window_has_to_be_long_enough_to_hold_a_baseline(self) -> None:
        self.check(baseline_window_days=3)

    def test_zero_sample_counts_are_refused(self) -> None:
        self.check(min_finding_samples=1)
        self.check(irregularity_min_intervals=1)
        self.check(max_evidence_points=1)

    def test_confidence_is_a_probability_and_cannot_be_certainty(self) -> None:
        self.check(confidence_ceiling=1.0)
        self.check(confidence_ceiling=0.0)
        self.check(provisional_confidence_cap=0.99, confidence_ceiling=0.9)

    def test_negative_durations_and_spreads_are_refused(self) -> None:
        self.check(min_sustained_minutes=-1)
        self.check(smoothing_minutes=-5)
        self.check(nocturnal_low_margin_bpm=-1)
        self.check(min_spread_bpm=0)

    def test_an_unknown_context_is_refused_wherever_it_is_written(self) -> None:
        self.check(contexts=("rest", "nap"))
        self.check(contexts=())
        self.check(exclude_exercise_in=("rest", "nap"))

    def test_the_shipped_defaults_pass_every_rule_above(self) -> None:
        config = AnomalyConfig()  # constructing it is the assertion
        self.assertLess(config.provisional_confidence_cap, config.confidence_ceiling)
        self.assertLessEqual(config.provisional_min_days, config.min_baseline_days)
        self.assertLessEqual(config.baseline_window_days, 28)


class DefaultsAreStatedTestCase(unittest.TestCase):
    """The defaults a reader of the docs will be told, pinned so a change is a decision."""

    def test_defaults_the_documentation_quotes(self) -> None:
        config = AnomalyConfig()
        self.assertEqual(config.min_baseline_days, 7)
        self.assertEqual(config.provisional_min_days, 3)
        self.assertEqual(config.confidence_ceiling, 0.95)
        self.assertEqual(config.provisional_confidence_cap, 0.55)
        self.assertEqual(config.min_sustained_minutes, 20.0)
        self.assertEqual(config.irregularity_block_minutes, 30.0)
        self.assertEqual(config.irregularity_floor, 0.15)
        # The floor constants exist because a measured spread of zero is a degenerate bar; if
        # either ever defaults to zero the rules fire on nothing.
        self.assertGreater(config.min_spread_bpm, 0.0)
        self.assertGreater(config.min_spread_index, 0.0)
