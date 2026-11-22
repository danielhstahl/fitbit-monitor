"""The documentation is a tested artifact, not a hope.

Three ways docs go stale, all of them checked here:

* the **API reference** drifting from the code - it is generated, and ``--check``
  fails the build when the committed block no longer matches;
* a **worked example** that no longer runs - the plugin in ``docs/plugins.md`` is
  executed against the same fixture store the contract suite uses;
* a **command line** that no longer parses - every ``fm`` invocation in the docs is
  parsed by the real argument parser.

Also checks the exit-code table matches the constants the runner actually returns,
and that the relative links in the docs point at files that exist.
"""

from __future__ import annotations

import importlib.util
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

from fitbit_monitor.cli import build_parser  # noqa: E402
from fitbit_monitor.plugins.runner import (  # noqa: E402
    EXIT_NO_STORE,
    EXIT_OK,
    EXIT_PLUGIN_ERROR,
    EXIT_USAGE,
)
from fitbit_monitor.plugins.testing import fixture_store, run_fixture_plugin  # noqa: E402

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
