"""The replay corpus is the generator's output, checked as code is checked.

``tests/fixtures/api`` is what every offline test of the API surface replays against, and
it is written by ``scripts/dev/record_fixtures.py`` - a documented stand-in for the Google
Health analyzer, not a scrape of the live service (ADR-0002 §10 step 1 is still open).
That makes two things worth asserting in CI rather than in a reviewer's memory:

* nobody hand-edited a fixture to make a test pass (the whole corpus regenerates
  byte-for-byte from the generator and its seed);
* the corpus still matches the *catalog* - a fixture for a data type the catalog no longer
  asks for is dead weight that will keep passing while the real request goes unrecorded.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fitbit_monitor.api.catalog import CATALOG
from fitbit_monitor.api.http import fixture_key, load_fixtures

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "api"
SCRIPT = REPO / "scripts" / "dev" / "record_fixtures.py"

SINCE = date(2026, 5, 1)
UNTIL = date(2026, 5, 14)


def generator():
    """The recording script, imported as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("record_fixtures", SCRIPT)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class CorpusIsTheGeneratorTest(unittest.TestCase):
    def test_the_committed_corpus_is_what_the_generator_writes(self) -> None:
        """Re-recording changes nothing - so a change here means someone edited a fixture.

        The fix is never to commit the edit: it is to change the generator (or the seed)
        so the stand-in answers that way, and re-record. Otherwise the corpus quietly stops
        being a description of the API and becomes a pile of things the tests expect.
        """
        with tempfile.TemporaryDirectory() as tmp:
            written = generator().build(Path(tmp))
            fresh = {path.name: json.loads(path.read_text()) for path in Path(tmp).glob("*.json")}
        committed = {path.name: json.loads(path.read_text()) for path in sorted(FIXTURES.glob("*.json"))}
        missing = sorted(set(fresh) - set(committed))
        stale = sorted(set(committed) - set(fresh))
        self.assertEqual(missing, [], f"generated but not committed; run: python {SCRIPT.relative_to(REPO)}")
        self.assertEqual(stale, [], f"committed but no longer generated; run: python {SCRIPT.relative_to(REPO)} --clean")
        self.assertGreater(len(written), 60, "the corpus is not empty by accident")
        for name in sorted(fresh):
            self.assertEqual(fresh[name], committed[name], f"{name} differs from the generator's output")

    def test_the_provenance_names_the_window_the_seed_and_the_honest_source(self) -> None:
        """The corpus says where it came from, and it does not claim to be the live API."""
        provenance = json.loads((FIXTURES / "_provenance.json").read_text())
        self.assertEqual(provenance["start"], SINCE.isoformat())
        self.assertEqual(provenance["stop"], UNTIL.isoformat())
        self.assertIsInstance(provenance["seed"], int)
        self.assertIsNone(provenance["recorded_from"], "no live capture has happened (ADR-0002 §10 step 1)")
        self.assertTrue(provenance["synthetic"])

    def test_provenance_is_metadata_and_the_replay_never_sees_it(self) -> None:
        fixtures = load_fixtures(FIXTURES)
        self.assertNotIn("GET /_provenance.json", fixtures)


class CorpusCoversTheCatalogTest(unittest.TestCase):
    def recorded_paths(self) -> set[str]:
        out: set[str] = set()
        for record in load_fixtures(FIXTURES).values():
            out.add(record["request"]["path"])
        return out

    def test_every_data_type_the_catalog_asks_for_is_answered_somewhere(self) -> None:
        """A dataset with no fixture is a dataset no test can exercise."""
        paths = self.recorded_paths()
        for name in CATALOG:
            asked = {
                request.path()
                for request in CATALOG[name].requests_in_chunk(SINCE, UNTIL)
            }
            self.assertTrue(
                asked & paths,
                f"{name} asks for {sorted(asked)} and nothing in the corpus answers any of them",
            )

    def test_no_fixture_belongs_to_a_data_type_the_catalog_forgot(self) -> None:
        """The reverse direction: a recording of an endpoint we no longer call.

        The account probes (`users/me/profile`, `users/me/identity`) are allowed to appear
        without being asked for here - the planner and the probe make those calls, not the
        catalog's dataset map. Anything else is a data type the catalog stopped asking for.
        """
        known = {request.path() for name in CATALOG for request in CATALOG[name].requests_in_chunk(SINCE, UNTIL)}
        probes = {"/users/me/profile", "/users/me/identity"}
        unaccounted = self.recorded_paths() - known - probes
        self.assertEqual(
            sorted(unaccounted),
            [],
            "recorded paths the catalog does not ask for in this window; a data type here is a "
            "catalog change, and the corpus is not where a catalog change gets made quietly",
        )

    def test_every_record_can_be_asked_for_again_by_key(self) -> None:
        """A record whose key does not round-trip would be a fixture nothing can find."""
        for key, record in load_fixtures(FIXTURES).items():
            request = record["request"]
            rebuilt = fixture_key(
                request["method"], request["path"], request.get("query") or {}, request.get("body")
            )
            self.assertEqual(rebuilt, key)


class StandInSpeaksTheFilterGrammarTest(unittest.TestCase):
    """The generator is only evidence while it refuses what the service refuses.

    v4's ``filter`` has two comparators, ``>=`` and ``<`` - "Supported comparison
    operators: ``>=``, ``<``", from the ``dataPoints`` list method of the published
    discovery document - and answers anything else with ``400 INVALID_ARGUMENT: Invalid
    data point filter: INVALID_DATA_POINT_FILTER_RESTRICTION_COMPARATOR``. The retired
    Fitbit grammar accepted ``<=``, so this is precisely the mistake a port makes quietly:
    one dataset warns, everything else imports, and the missing rows are invisible unless
    someone counts them.
    """

    TYPE = "daily-heart-rate-variability"
    FIELD = "daily_heart_rate_variability.date"

    def setUp(self) -> None:
        self.api = generator().SyntheticApi(start=SINCE, stop=UNTIL)

    def ask(self, expression: str) -> tuple[int, dict]:
        return self.api.respond(
            "GET",
            f"/users/me/dataTypes/{self.TYPE}/dataPoints",
            {"filter": expression, "pageSize": "10000"},
            {},
        )

    def days(self, payload: dict) -> list[int]:
        """The days answered, out of the camelCase attribute the response uses."""
        return sorted(point["dailyHeartRateVariability"]["date"]["day"] for point in payload["dataPoints"])

    def test_an_inclusive_upper_bound_is_refused_the_way_the_service_refuses_it(self) -> None:
        status, payload = self.ask(f'{self.FIELD} >= "2026-05-01" AND {self.FIELD} <= "2026-05-14"')
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["status"], "INVALID_ARGUMENT")
        self.assertEqual(
            payload["error"]["message"],
            "Invalid data point filter: INVALID_DATA_POINT_FILTER_RESTRICTION_COMPARATOR",
        )

    def test_the_exclusive_bound_answers_the_days_before_it_and_not_the_day_it_names(self) -> None:
        status, payload = self.ask(f'{self.FIELD} >= "2026-05-01" AND {self.FIELD} < "2026-05-08"')
        self.assertEqual(status, 200)
        self.assertEqual(self.days(payload), [1, 2, 3, 4, 5, 6, 7])

    def test_a_single_day_window_is_one_day(self) -> None:
        status, payload = self.ask(f'{self.FIELD} >= "2026-05-06" AND {self.FIELD} < "2026-05-07"')
        self.assertEqual(status, 200)
        self.assertEqual(self.days(payload), [6])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
