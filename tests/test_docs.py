"""The documentation is a tested artifact, not a hope.

Three ways docs go stale, all of them checked here:

* the **API reference** drifting from the code - it is generated, and ``--check``
  fails the build when the committed block no longer matches;
* a **worked example** that no longer runs - the plugin in ``docs/plugins.md`` is
  executed against the same fixture store the contract suite uses;
* a **command line** that no longer parses - every ``fm`` invocation in the docs is
  parsed by the real argument parser;
* a **decision** that quietly stopped matching the code - ADR-0004 pins the plugin
  contract, so every plugin-facing name in the code must be named in it.

Also checks the exit-code table matches the constants the runner actually returns,
and that the relative links in the docs point at files that exist.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import io
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fitbit_monitor.api.catalog import CATALOG  # noqa: E402
from fitbit_monitor.cli import build_parser  # noqa: E402
from fitbit_monitor.plugins.contract import (  # noqa: E402
    MIN_PLUGIN_API_VERSION,
    PLUGIN_API_VERSION,
    MonitorPlugin,
    PluginContext,
)
from fitbit_monitor.plugins.errors import STAGES  # noqa: E402
from fitbit_monitor.plugins.models import Alert, Evidence, MetricResult  # noqa: E402
from fitbit_monitor.plugins.runner import (  # noqa: E402
    DEFAULT_TIMEOUT_SECONDS,
    EXIT_NO_STORE,
    EXIT_OK,
    EXIT_PLUGIN_ERROR,
    EXIT_USAGE,
    STATUS_CONFIG_ERROR,
    STATUS_ERROR,
    STATUS_NO_DATA,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
)
from fitbit_monitor.plugins import testing as plugin_testing  # noqa: E402
from fitbit_monitor.plugins.testing import (  # noqa: E402
    FIXTURE_DAYS,
    FIXTURE_END,
    fixture_store,
    run_fixture_plugin,
)
from fitbit_monitor.store.reader import ReadOnlyStore  # noqa: E402

DOCS = REPO / "docs"
SCRIPTS = REPO / "scripts"


def _load_api_reference_module():
    spec = importlib.util.spec_from_file_location("api_reference", SCRIPTS / "docs" / "api_reference.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _fenced_blocks(text: str, language: str) -> list[str]:
    pattern = re.compile(rf"```{language}\n(.*?)```", re.DOTALL)
    return pattern.findall(text)


def _fm_commands(text: str) -> list[list[str]]:
    """Every ``fm ...`` invocation in the docs, as argument lists."""
    out: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("$").strip()
        if not stripped.startswith("fm "):
            continue
        stripped = re.split(r"[|#>]", stripped)[0].strip()
        argv = [token for token in shlex.split(stripped)[1:] if "$" not in token]
        out.append(argv)
    return out


class ApiReferenceIsGenerated(unittest.TestCase):
    def test_generated_block_in_the_docs_is_up_to_date(self) -> None:
        module = _load_api_reference_module()
        doc = (DOCS / "plugins.md").read_text()
        self.assertIn(module.BEGIN, doc, "docs/plugins.md lost its generated-block markers")
        regenerated = module.splice(doc, module.render())
        self.assertEqual(
            regenerated,
            doc,
            "docs/plugins.md API reference is stale: python scripts/docs/api_reference.py --write",
        )

    def test_reference_covers_the_public_symbols(self) -> None:
        module = _load_api_reference_module()
        block = module.render()
        for symbol in (
            "MonitorPlugin",
            "PluginContext",
            "PluginOption",
            "MetricResult",
            "Alert",
            "Evidence",
            "Severity",
            "run_plugins",
            "PLUGIN_API_VERSION",
            "ENTRY_POINT_GROUP",
        ):
            self.assertIn(symbol, block)
        self.assertIn("downsample", block)

    def test_doc_writer_returns_a_clean_diff_free_result(self) -> None:
        module = _load_api_reference_module()
        self.assertEqual(module.main(["--check", "--doc", str(DOCS / "plugins.md")]), 0)


class WorkedExampleRuns(unittest.TestCase):
    """The contract for the example is: a reader can copy it and it works."""

    def _example_class(self):
        text = (DOCS / "plugins.md").read_text()
        for block in _fenced_blocks(text, "python"):
            if "RestingVsStepsPlugin" in block and "class RestingVsStepsPlugin" in block:
                namespace: dict = {"__name__": "docs_example"}
                exec(compile(block, "docs/plugins.md#example", "exec"), namespace)
                return namespace["RestingVsStepsPlugin"]
        self.fail("docs/plugins.md no longer contains the runnable plugin example")

    def test_the_documented_plugin_loads_and_runs(self) -> None:
        plugin_cls = self._example_class()
        with tempfile.TemporaryDirectory() as tmp:
            store_path = fixture_store(tmp)
            outcome = run_fixture_plugin(plugin_cls, store_path=store_path)
        self.assertEqual(outcome.status, "ok", outcome.error)
        self.assertEqual(outcome.metrics[0].key, "resting_hr_per_10k_steps")
        self.assertEqual(outcome.metrics[0].unit, "bpm per 10k steps")
        self.assertLessEqual(outcome.metrics[0].confidence, 1.0)

    def test_the_documented_option_takes_effect(self) -> None:
        plugin_cls = self._example_class()
        with tempfile.TemporaryDirectory() as tmp:
            store_path = fixture_store(tmp)
            outcome = run_fixture_plugin(
                plugin_cls, store_path=store_path, config_values={"min_days": 10_000}
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.metrics, [], "min_days too high should mean no metric, not a crash")


class ReferenceExampleIsQuoted(unittest.TestCase):
    """The reference metric plugin's docstring *is* the worked example in the docs.

    The ticket asked for "a docstring short enough to serve as the reference example in
    ``docs/plugins.md``", so the docs print it in full. Two ways that rots: the quote
    drifts from the code, or the docstring grows into an essay nobody quotes. Both fail
    here.
    """

    @staticmethod
    def _docstring() -> str:
        from fitbit_monitor.plugins.builtin import resting_trend

        return inspect.cleandoc(resting_trend.__doc__ or "")

    def test_the_docs_quote_the_reference_docstring_verbatim(self) -> None:
        doc = self._docstring()
        self.assertTrue(doc, "resting_trend lost the module docstring the docs quote")
        text = (DOCS / "plugins.md").read_text()
        self.assertIn(
            doc,
            text,
            "docs/plugins.md no longer quotes resting_trend's module docstring verbatim",
        )

    def test_the_quoted_docstring_stays_short_enough_to_quote(self) -> None:
        doc = self._docstring()
        self.assertLessEqual(
            len(doc.splitlines()),
            28,
            "the reference example has to stay readable in one screen; the hr-anomaly "
            "docstring is the place for long-form design notes, not this one",
        )
        self.assertLessEqual(len(doc), 1_500, "same ratchet, measured in characters")

    def test_the_reference_metric_keys_are_all_documented(self) -> None:
        """Every metric key the reference plugin emits appears in the docs table."""
        from fitbit_monitor.plugins.builtin.resting_trend import RestingTrendPlugin

        outcome = run_fixture_plugin(
            RestingTrendPlugin,
            store_path=fixture_store(tempfile.mkdtemp(prefix="fm-docs-")),
        )
        text = (DOCS / "plugins.md").read_text()
        for key in sorted({m.key for m in outcome.metrics}):
            self.assertIn(f"`{key}`", text, f"docs/plugins.md does not document {key}")


class DocumentedCommandsParse(unittest.TestCase):
    def test_every_fm_command_in_the_docs_is_a_real_command(self) -> None:
        parser = build_parser()
        documents = [REPO / "README.md"] + sorted(DOCS.rglob("*.md"))
        seen = 0
        for document in documents:
            for argv in _fm_commands(document.read_text()):
                with self.subTest(document=document.name, argv=" ".join(argv)):
                    try:
                        parser.parse_args(argv)
                    except SystemExit as exc:  # pragma: no cover - the assertion is the point
                        self.fail(f"docs advertise `fm {' '.join(argv)}` which the parser rejects ({exc})")
                    seen += 1
        self.assertGreater(seen, 15, "the docs should be showing more commands than this")


class DocumentedExitCodes(unittest.TestCase):
    def test_exit_code_tables_match_the_runner(self) -> None:
        expected = {
            str(EXIT_OK): "success",
            str(EXIT_PLUGIN_ERROR): "plugin",
            str(EXIT_USAGE): "arguments",
            str(EXIT_NO_STORE): "store",
        }
        for document in (DOCS / "plugins.md", REPO / "README.md"):
            text = document.read_text()
            for code, keyword in expected.items():
                if f"| {code} " not in text:
                    continue
                line = next(line for line in text.splitlines() if line.startswith(f"| {code} "))
                self.assertIn(keyword, line.lower(), f"{document.name}: exit {line!r}")


class LinksAndFacts(unittest.TestCase):
    def test_relative_links_in_the_docs_resolve(self) -> None:
        pattern = re.compile(r"\]\((?!https?:)([^)#]+)")
        checked = 0
        for document in [REPO / "README.md"] + sorted(DOCS.rglob("*.md")):
            for target in pattern.findall(document.read_text()):
                resolved = (document.parent / target).resolve()
                self.assertTrue(resolved.exists(), f"{document.name}: broken link -> {target}")
                checked += 1
        self.assertGreater(checked, 3)

    def test_the_entry_point_group_named_in_the_docs_is_the_real_one(self) -> None:
        from fitbit_monitor.plugins import ENTRY_POINT_GROUP

        for name in ("plugins.md", "adr/0004-plugin-contract.md"):
            self.assertIn(
                ENTRY_POINT_GROUP,
                (DOCS / name).read_text(),
                f"{name} must name the entry-point group exactly",
            )

    def test_the_docs_state_the_read_only_and_https_promises(self) -> None:
        self.assertIn("mode=ro", (DOCS / "adr" / "0004-plugin-contract.md").read_text())
        policy = (DOCS / "data-policy.md").read_text().lower()
        self.assertIn("https", policy)
        self.assertIn("webhook", policy)
        self.assertIn("no telemetry", policy)

    def test_scaffold_documents_match_the_generator(self) -> None:
        from fitbit_monitor.plugins.scaffold import _README

        readme = _README.format(
            cls="XPlugin", name="x", dataset="daily_summary", field="resting_hr"
        )
        for line in ("fm plugins list --dir", "fm plugins run x", "PluginContractTestCase"):
            self.assertIn(line, readme)

    def test_the_docs_draw_the_project_the_scaffold_actually_writes(self) -> None:
        """docs/plugins.md shows the scaffolded tree; a renamed file has to fail here."""
        from fitbit_monitor.plugins.scaffold import scaffold_plugin

        with tempfile.TemporaryDirectory() as tmp:
            scaffold = scaffold_plugin("resting_vs_steps", tmp, dry_run=True)
            self.assertFalse((Path(tmp) / "resting_vs_steps").exists(), "--dry-run wrote something")
        text = (DOCS / "plugins.md").read_text()
        for relative in scaffold.files:
            self.assertIn(
                Path(relative).name,
                text,
                f"docs/plugins.md does not mention the scaffolded file {relative}",
            )
        for command in ("fm plugin new", "--dry-run", "fm plugins list --dir ."):
            self.assertIn(command, text, f"docs/plugins.md does not document {command}")


class HealthSourceIsDecidedOnce(unittest.TestCase):
    """ADR-0002 is where the data source gets decided, so it has to stay concrete.

    The point of the file is that Epic 2 (client + OAuth) and Epic 4 (archive import)
    need no further source decisions, which means the answers have to be *in* it: the
    host, the scopes, the limits, the access path, the fallback, and the adapter rule.
    """

    ADR = DOCS / "adr" / "0002-health-api-source.md"
    DECIDED_SCOPES = (
        "health_metrics_and_measurements.readonly",
        "activity_and_fitness.readonly",
        "sleep.readonly",
        "profile.readonly",
    )

    def test_the_adr_exists_and_is_accepted(self) -> None:
        self.assertTrue(self.ADR.is_file(), "docs/adr/0002-health-api-source.md is missing")
        self.assertIn("- **Status:** accepted", self.ADR.read_text())
        self.assertIn("0002-health-api-source.md", (REPO / "README.md").read_text())

    def test_it_records_what_is_dead_and_what_replaced_it(self) -> None:
        text = self.ADR.read_text()
        self.assertIn("health.googleapis.com/v4", text, "the decided host is missing")
        self.assertIn("api.fitbit.com", text, "the rejected legacy host is not named")
        self.assertIn("Google Fit", text, "the other candidate is not answered")
        self.assertIn("2026-11-02", text, "the legacy turn-off date is not recorded")

    def test_it_names_the_scope_set_and_what_a_missing_one_costs(self) -> None:
        text = self.ADR.read_text()
        self.assertIn("https://www.googleapis.com/auth/googlehealth", text)
        for scope in self.DECIDED_SCOPES:
            self.assertIn(scope, text, f"{scope} is not in the decided scope table")
        self.assertIn("MISSING_OAUTH_SCOPE", text, "the refusal without a scope is not recorded")

    def test_it_records_the_limits_a_client_has_to_obey(self) -> None:
        text = self.ADR.read_text()
        for fact in ("300 requests", "10 000", "14 days", "90 days", "RESOURCE_EXHAUSTED"):
            self.assertIn(fact, text, f"{fact} belongs in the limits section")

    def test_it_answers_the_personal_access_question_and_keeps_the_archive_first_class(self) -> None:
        text = self.ADR.read_text()
        self.assertIn("Personal mode", text)
        self.assertIn("Takeout", text)
        self.assertIn("class Source(Protocol)", text, "the adapter interface is the decision")

    def test_it_maps_every_dataset_the_catalog_imports(self) -> None:
        """A dataset the code can import but the ADR does not place is an undecided source."""
        text = self.ADR.read_text()
        for name in sorted(CATALOG):
            self.assertIn(f"`{name}`", text, f"ADR-0002 §7 does not place the {name} dataset")


class PluginContractIsDecidedOnce(unittest.TestCase):
    """ADR-0004 is where the plugin contract gets pinned, so it has to stay exact.

    Epics 5 and 6 write against this file, which means the answers have to be *in*
    it: the base-class signatures, every field of the emitted models, the read API,
    discovery and precedence, configuration, versioning, isolation, the conformance
    suite, and a sample plugin a reader can copy. Each check is driven from the code,
    so a plugin-facing addition that skips the ADR fails - the same way the generated
    API reference fails when it goes stale.
    """

    ADR = DOCS / "adr" / "0004-plugin-contract.md"
    #: Plumbing rather than contract: the lifecycle call and the raw dataclass field.
    STORE_PLUMBING = {"close", "connection"}

    @classmethod
    def text(cls) -> str:
        return cls.ADR.read_text(encoding="utf-8")

    @classmethod
    def sample(cls) -> str:
        for block in _fenced_blocks(cls.text(), "python"):
            if "class RestingHrDrift" in block:
                return block
        raise cls.failureException("ADR-0004 no longer contains the sample plugin")

    @classmethod
    def sample_class(cls):
        namespace: dict = {"__name__": "adr_0004_sample"}
        exec(compile(cls.sample(), "adr/0004-plugin-contract.md#sample", "exec"), namespace)
        return namespace["RestingHrDrift"]

    def test_the_adr_exists_and_is_accepted(self) -> None:
        self.assertTrue(self.ADR.is_file(), "docs/adr/0004-plugin-contract.md is missing")
        self.assertIn("- **Status:** accepted", self.text())
        self.assertIn("0004-plugin-contract.md", (REPO / "README.md").read_text())

    def test_it_gives_the_exact_lifecycle_signatures(self) -> None:
        text = self.text()
        for method in (MonitorPlugin.configure, MonitorPlugin.run):
            signature = re.sub(r"\s+", " ", str(inspect.signature(method)).replace("'", ""))
            self.assertIn(
                signature,
                text,
                f"ADR-0004 must give configure()/run() exactly as implemented: {signature}",
            )

    def test_it_gives_the_emission_signatures(self) -> None:
        text = self.text()
        for needle in (
            "def metric(self, key: str, value: float | int | str, *",
            "def alert(self, key: str, title: str, message: str, *",
            "occurred_at: datetime",
            "severity: Severity | str = Severity.WARNING",
            "dedup_key: str | None = None",
        ):
            self.assertIn(needle, text, f"ADR-0004 §3 does not spell out `{needle}`")

    def test_it_tabulates_every_field_of_the_returned_models(self) -> None:
        """A field a plugin can emit but the ADR does not tabulate is undecided."""
        text = self.text()
        for model in (MetricResult, Alert, Evidence):
            for field in dataclasses.fields(model):
                self.assertIn(
                    f"| `{field.name}` |",
                    text,
                    f"ADR-0004 §4 has no row for {model.__name__}.{field.name}",
                )

    def test_it_names_both_kinds_of_plugin(self) -> None:
        text = self.text()
        for kind in ("metric-producing", "alerting", "`MetricResult`", "`Alert`"):
            self.assertIn(kind, text, f"ADR-0004 §1 does not name the {kind} kind")

    def test_it_records_the_validation_rules(self) -> None:
        text = self.text()
        for needle in (
            '"info"',
            '"warning"',
            '"critical"',
            "0.0 .. 1.0",
            "chronological",
            "naive",
            "must not precede",
            "tz-aware",
            "frozen dataclasses",
        ):
            self.assertIn(needle, text, f"ADR-0004 §4 does not record the {needle!r} rule")

    def test_it_names_every_read_only_accessor(self) -> None:
        text = self.text()
        for name in sorted(n for n in dir(ReadOnlyStore) if not n.startswith("_")):
            if name in self.STORE_PLUMBING:
                continue
            self.assertIn(name, text, f"ADR-0004 §3 does not document ReadOnlyStore.{name}")

    def test_it_names_every_context_member(self) -> None:
        text = self.text()
        for name in sorted(n for n in dir(PluginContext) if not n.startswith("_")):
            self.assertIn(f"`{name}", text, f"ADR-0004 §3 does not document PluginContext.{name}")

    def test_it_names_every_failure_stage_and_status(self) -> None:
        text = self.text()
        for name in (
            "PluginError",
            "PluginImportError",
            "PluginRegistrationError",
            "PluginConfigError",
            "PluginRuntimeError",
            "PluginTimeout",
        ):
            self.assertIn(name, text, f"ADR-0004 §8 does not name {name}")
        for stage in STAGES:
            self.assertIn(stage, text, f"ADR-0004 §8 does not name the {stage} stage")
        for status in (
            STATUS_OK,
            STATUS_NO_DATA,
            STATUS_SKIPPED,
            STATUS_CONFIG_ERROR,
            STATUS_ERROR,
            STATUS_TIMEOUT,
        ):
            self.assertIn(f"`{status}`", text, f"ADR-0004 §8 does not report the {status} status")
        for code in (EXIT_OK, EXIT_PLUGIN_ERROR, EXIT_USAGE, EXIT_NO_STORE):
            self.assertIn(f"| `{code}` |", text, f"ADR-0004 §8 does not document exit code {code}")

    def test_it_fixes_the_discovery_sources_and_precedence(self) -> None:
        from fitbit_monitor.plugins import ENTRY_POINT_GROUP

        text = self.text()
        self.assertIn(ENTRY_POINT_GROUP, text, "the entry-point group must be named exactly")
        for needle in (
            "[plugins]",
            "dirs",
            "FM_PLUGIN_DIRS",
            "*.py",
            "<pkg>/plugin.py",
            "directory` > `entry_point` > `builtin",
            "collision",
            "entry_point",
            "builtin",
        ):
            self.assertIn(needle, text, f"ADR-0004 §5 does not decide {needle!r}")

    def test_it_states_the_no_write_rule_and_how_it_is_enforced(self) -> None:
        text = self.text()
        for needle in ("mode=ro", "PRAGMA query_only=ON", "MUST NOT write", "sqlite3.OperationalError"):
            self.assertIn(needle, text, f"ADR-0004 §3 must state {needle!r}")

    def test_it_pins_the_version_policy_to_the_code_constants(self) -> None:
        text = self.text()
        self.assertIn(f"PLUGIN_API_VERSION: Final[int] = {PLUGIN_API_VERSION}", text)
        self.assertIn(f"MIN_PLUGIN_API_VERSION: Final[int] = {MIN_PLUGIN_API_VERSION}", text)
        for needle in ("requires_api", "needs plugin API", "dropped", "fm plugins --api", "additive"):
            self.assertIn(needle, text, f"ADR-0004 §7 does not decide {needle!r}")

    def test_it_names_the_budget_the_runner_actually_uses(self) -> None:
        text = self.text()
        self.assertIn(f"DEFAULT_TIMEOUT_SECONDS = {DEFAULT_TIMEOUT_SECONDS:g}", text)
        for needle in ("timeout_seconds", "--timeout", "daemon", "check_timeout", "cooldown", "36h"):
            self.assertIn(needle, text, f"ADR-0004 §8/§11 does not name {needle!r}")

    def test_it_enumerates_the_contract_suite_the_code_publishes(self) -> None:
        text = self.text()
        self.assertIn("fitbit_monitor.plugins.testing", text)
        self.assertIn("PluginContractTestCase", text)
        for needle in ("fixture_store", "fixture_window", "empty_window", "run_fixture_plugin"):
            self.assertIn(needle, text, f"ADR-0004 §9 does not publish {needle}()")
        self.assertIn(f"{FIXTURE_DAYS} days", text, "the fixture length is not recorded")
        self.assertIn(FIXTURE_END.isoformat(), text, "the fixture end date is not recorded")
        # The scaffold is how an author meets the suite, so its shape is decided here too.
        self.assertIn("<name>.plugin:<Class>", text, "ADR-0004 §9 does not pin the scaffolded entry point")
        self.assertIn("[tool.setuptools] packages", text, "ADR-0004 §9 does not pin the scaffolded layout")

    def test_it_documents_how_config_reaches_a_plugin(self) -> None:
        text = self.text()
        for needle in (
            "[plugins.<name>]",
            "PluginOption",
            "int | float | bool | str | duration | list",
            "unknown_keys",
            "unused config keys",
            "enabled",
            "configure()",
            "datasets",
        ):
            self.assertIn(needle, text, f"ADR-0004 §6 does not decide {needle!r}")

    def test_the_sample_plugin_is_twenty_lines_or_fewer(self) -> None:
        lines = self.sample().rstrip("\n").split("\n")
        self.assertLessEqual(len(lines), 20, f"the ADR sample is {len(lines)} lines: trim it")

    def test_the_sample_plugin_runs_and_reports_both_kinds(self) -> None:
        plugin_cls = self.sample_class()
        with tempfile.TemporaryDirectory() as tmp:
            store_path = fixture_store(tmp)
            quiet = run_fixture_plugin(plugin_cls, store_path=store_path)
            loud = run_fixture_plugin(plugin_cls, store_path=store_path, config_values={"high_bpm": 50.0})
        self.assertEqual(quiet.status, STATUS_OK, quiet.error)
        self.assertEqual([m.key for m in quiet.metrics], ["resting_hr_mean"])
        self.assertEqual(quiet.alerts, [], "the default threshold must not alert on the fixture")
        self.assertEqual(loud.status, STATUS_OK, loud.error)
        self.assertEqual(len(loud.alerts), 1, "lowering the option must produce the alert")
        alert = loud.alerts[0]
        self.assertEqual(alert.key, "high")
        self.assertEqual(alert.severity.value, "warning")
        self.assertIsNotNone(alert.occurred_at.tzinfo)
        self.assertTrue(alert.evidence and alert.evidence[0].at.tzinfo, "evidence needs a tz-aware timestamp")

    def test_the_sample_plugin_passes_the_contract_suite(self) -> None:
        """The ADR's own example is held to the suite it tells authors to run."""
        suite_cls = type(
            "AdrSampleConformance",
            (plugin_testing.PluginContractTestCase,),
            {"plugin_cls": self.sample_class()},
        )
        suite = unittest.TestLoader().loadTestsFromTestCase(suite_cls)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful(), "the ADR sample fails the suite it ships")


class NightlyDetectorIsDecidedOnce(unittest.TestCase):
    """ADR-0006 decides why nightly HRV is a second detector, so it must stay true.

    The file earns its place by recording things a later reader would otherwise get
    wrong: which rule ids and defaults shipped, the two guards that stop a baseline
    eating itself, what is shared with `hr-anomaly` on purpose, and - most usefully -
    what is still unmeasured. Every check here is driven from the code, so retuning a
    default, renaming a rule or quietly deleting the open question all fail here.
    """

    ADR = DOCS / "adr" / "0006-nightly-series-detector.md"

    #: The phrases that name the two guards. They are the decision in eight words each;
    #: losing one means someone restructured the scan and forgot why it is shaped so.
    GUARDS = (
        "does not join the pool the bar is built from",
        "of lead-in",
    )

    def test_the_adr_exists_and_is_accepted(self) -> None:
        self.assertTrue(self.ADR.is_file(), "docs/adr/0006-nightly-series-detector.md is missing")
        self.assertIn("- **Status:** accepted", self.ADR.read_text(encoding="utf-8"))
        self.assertIn("0006-nightly-series-detector.md", (REPO / "README.md").read_text())

    def test_it_names_the_rules_exactly_as_the_detector_exports_them(self) -> None:
        from fitbit_monitor.anomaly.hrv import FINDING_HRV_HIGH, FINDING_HRV_LOW

        text = self.ADR.read_text(encoding="utf-8")
        for rule in (FINDING_HRV_LOW, FINDING_HRV_HIGH):
            self.assertIn(f"`{rule}`", text, f"{rule} is not in the rules table")

    def test_the_defaults_it_quotes_are_the_ones_that_ship(self) -> None:
        """The rules table may be prose, but it may not be wrong."""
        from fitbit_monitor.anomaly.hrv import FINDING_HRV_LOW, HrvAnomalyConfig

        config = HrvAnomalyConfig()
        text = self.ADR.read_text(encoding="utf-8")
        row = next((line for line in text.splitlines() if line.startswith(f"| `{FINDING_HRV_LOW}`")), None)
        self.assertIsNotNone(row, "the hrv_low row is missing from the rules table")
        for value in (config.min_consecutive_nights, config.z_low, config.deep_dip_z):
            self.assertIn(f"{value:g}", row, f"{value:g} is not in the hrv_low row")
        self.assertIn("alert_on_high = false", text)
        self.assertFalse(config.alert_on_high, "the ADR says the high rule ships off")
        self.assertIn("corroborate = true", text)
        self.assertTrue(config.corroborate, "the ADR says corroboration ships on")

    def test_it_records_the_two_ways_a_baseline_eats_itself(self) -> None:
        text = self.ADR.read_text(encoding="utf-8")
        for phrase in self.GUARDS:
            self.assertIn(phrase, text, f"the guard '{phrase}' is no longer recorded")

    def test_what_it_calls_shared_really_is_shared(self) -> None:
        """It claims four imports from `hr-anomaly`; if one stops existing, the claim rots."""
        from fitbit_monitor.plugins.builtin import hr_anomaly

        source = ""
        from fitbit_monitor.plugins.builtin import hrv_anomaly

        source = Path(hrv_anomaly.__file__).read_text(encoding="utf-8")
        for name in ("thin_window_reason", "_fraction", "_duration", "HeldBack"):
            self.assertTrue(hasattr(hr_anomaly, name), f"ADR-0006 says {name} is reused; it is gone")
            self.assertIn(name, source, f"hrv_anomaly no longer imports {name}")

    def test_the_framing_sentence_is_its_own_even_though_the_code_is_shared(self) -> None:
        """Shared disclaimer over a different signal is the decoration this avoids."""
        from fitbit_monitor.plugins.builtin.hr_anomaly import NON_DIAGNOSTIC, NON_DIAGNOSTIC_CODE
        from fitbit_monitor.plugins.builtin.hrv_anomaly import (
            HRV_FRAMING_CODE,
            NON_DIAGNOSTIC_HRV,
        )

        self.assertEqual(HRV_FRAMING_CODE, NON_DIAGNOSTIC_CODE)
        self.assertNotEqual(NON_DIAGNOSTIC_HRV, NON_DIAGNOSTIC)
        self.assertIn("one HRV number per night", NON_DIAGNOSTIC_HRV)

    def test_it_still_says_what_is_not_measured(self) -> None:
        """The accuracy gate is the open question; deleting the paragraph is not closing it.

        If someone does build a labelled nightly corpus, this test is the reminder that the
        ADR has to change as well - which is the whole point of writing the gap down.
        """
        text = self.ADR.read_text(encoding="utf-8")
        fixtures = list((REPO / "tests" / "fixtures" / "anomaly").glob("*hrv*"))
        if fixtures:
            self.assertNotIn(
                "no such gate yet", text,
                f"{fixtures} exist now: ADR-0006 still claims the nightly detector is unmeasured",
            )
        else:
            self.assertIn("no such gate yet", text, "ADR-0006 must state the missing accuracy gate")
            self.assertIn("accuracy gate", text)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
