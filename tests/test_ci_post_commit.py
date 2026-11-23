"""The post-commit runner: workspace resolution, the act-mcp call, and one summary block.

``scripts/ci/post_commit.sh`` is the thing that runs after every commit - the coverage
job and the benchmark job, either through ``act-mcp``/``act`` or by calling the gate
scripts directly - and it exists because of a failure this workspace actually logged:

    act-mcp: workspace /Users/.../fitbit-monitor has no `.github` directory ...
      fix: point --workspace at the repository root that contains .github/workflows/*.yml

So the properties below are about the three ways that script can be wrong:

* **it runs against the wrong directory** - so the workspace it resolves is checked from
  a foreign cwd, from an explicit ``--workspace``, and from a tree with no workflows at
  all (where the only acceptable answer is exit 2 and a ``fix:`` line naming what it tried);
* **it reports a number it did not measure** - so ``scripts/ci/_gate_summary.py`` is fed
  synthetic reports and logs, and a block that prints a percentage below its floor, or a
  regressed case, has to say FAIL and exit nonzero;
* **it claims a container ran that never did** - so a stub MCP server (the transport
  ``act-mcp`` speaks: JSON-RPC over Streamable HTTP) records the arguments the client
  sent, and the tests assert the repository root arrived as the workspace argument, and
  that a server that cannot be reached sends the run back to the gate scripts.

Nothing here runs the real gates: the stub gate scripts write the reports the summary
reads, so the whole module runs in about a second. That is a property, not a convenience
- ``scripts/ci/coverage.sh`` runs this suite and this module runs the runner, so a run
that resolved back to the real repository would call the gate that calls this suite for
ever. ``scripts/ci/post_commit.sh`` refuses exactly that nesting.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import re
import io
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
POST_COMMIT = REPO / "scripts" / "ci" / "post_commit.sh"
HOOK = REPO / "scripts" / "git-hooks" / "post-commit"
HOOK_INSTALLER = REPO / "scripts" / "git-hooks" / "install.sh"
ACT_MCP = REPO / "scripts" / "ci" / "_act_mcp.py"
SUMMARY = REPO / "scripts" / "ci" / "_gate_summary.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


summary = _load("_gate_summary", SUMMARY)
act_mcp = _load("_act_mcp", ACT_MCP)


# --- the fake act-mcp ----------------------------------------------------------
class StubMcpHandler(http.server.BaseHTTPRequestHandler):
    """The slice of MCP a workflow runner uses: initialize, tools/list, tools/call.

    ``server.tools`` and ``server.calls`` are what the tests assert on: the catalogue the
    client was offered, and the arguments it actually sent when it ran a job.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # pragma: no cover - keep the test output readable
        pass

    def _send(self, payload: str, *, sse: bool = False, status: int = 200) -> None:
        body = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", "stub-session")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length) or b"{}")
        server = self.server
        server.sessions.append(self.headers.get("Mcp-Session-Id"))
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self._send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "serverInfo": {"name": "act-mcp", "version": "stub"},
                            "capabilities": {"tools": {}},
                        },
                    }
                )
            )
        elif method == "notifications/initialized":
            self._send("", status=202)
        elif method == "tools/list":
            self._send(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"tools": server.tools}}))
        elif method == "tools/call":
            params = message.get("params") or {}
            server.calls.append(params)
            result: dict[str, Any] = {
                "content": [{"type": "text", "text": server.job_output}],
                "isError": server.job_is_error,
            }
            payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
            self._send(payload, sse=getattr(server, "sse", False))
        else:
            self._send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"no {method}"}}
                )
            )


class StubMcpServer:
    """A real socket on localhost, so the transport the client uses is the thing under test."""

    def __init__(self, tools: list[dict[str, Any]], job_output: str = "", *, sse: bool = False) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubMcpHandler)
        self.server.tools = tools
        self.server.job_output = job_output
        self.server.job_is_error = False
        self.server.calls: list[dict[str, Any]] = []
        self.server.sessions: list[str | None] = []
        self.server.sse = sse
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/mcp"

    def __enter__(self) -> "StubMcpServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


RUN_WORKFLOW_TOOL = {
    "name": "run_workflow",
    "description": "Run one job of a workflow in the given workspace",
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
            "job": {"type": "string"},
            "workflow": {"type": "string"},
        },
        "required": ["workspace"],
    },
}

#: A server whose tool cannot be told which repository to run - the case the client must
#: refuse instead of silently dropping the one argument that was the whole point.
NO_WORKSPACE_TOOL = {
    "name": "run_workflow",
    "inputSchema": {"type": "object", "properties": {"job": {"type": "string"}, "workflow": {"type": "string"}}},
}

COVERAGE_JOB_LOG = "\n".join(
    [
        "== test suite",
        "Ran 433 tests",
        "OK",
        "coverage: 92.0%",
        "coverage overall 92.0% (floor 85.0%) ok",
        "artifact coverage-xml: coverage.xml",
    ]
)

BENCH_JOB_LOG = "\n".join(
    [
        "plugin-system benchmarks (90 days, 9 rounds; best sample gates)",
        "  seed_corpus   125,251.9 rows/sec  -2.3% vs baseline",
        "no regressions beyond 15%",
        "took 4.1s",
    ]
)


# --- a repository stand-in -----------------------------------------------------
def make_repo(tmp: Path) -> Path:
    """A tree that looks like the repository to the runner, with stub gate scripts.

    The stubs write the same reports the real gates write - which is all the summary
    block reads - and append to ``$CALL_LOG`` so a test can see what actually ran.
    """
    (tmp / ".github" / "workflows").mkdir(parents=True)
    for name in ("ci.yml", "bench.yml"):
        (tmp / ".github" / "workflows" / name).write_text(f"# {name}\n")
    (tmp / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "_python.sh", tmp / "scripts" / "_python.sh")
    for name in ("_gate_summary.py", "_act_mcp.py", "_coverage_report.py", "post_commit.sh"):
        shutil.copy(REPO / "scripts" / "ci" / name, tmp / "scripts" / "ci" / name)
    (tmp / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 85\n")
    coverage_json = {
        "totals": {
            "num_statements": 100,
            "covered_lines": 92,
            "num_branches": 0,
            "covered_branches": 0,
        },
        "files": {},
    }
    bench_json = {
        "cases": [{"case": "seed_corpus", "tracked": True, "regression_percent": 2.0}],
        "gate": {
            "gated": True,
            "passed": True,
            "tracked_cases": 1,
            "regressed_cases": [],
            "tolerance_percent": 15.0,
        },
    }
    stubs = {
        "lint.sh": 'echo "stub lint"\n',
        "coverage.sh": "echo 'stub coverage'\n"
        "printf '%s' '" + json.dumps(coverage_json) + "' > coverage.json\n"
        "echo 'coverage: 92.0%'\n"
        "echo 'coverage overall 92.0% (floor 85.0%) ok'\n",
        "bench.sh": "echo 'stub bench'\n"
        "printf '%s' '" + json.dumps(bench_json) + "' > bench-results.json\n"
        "echo 'no regressions beyond 15%'\n",
    }
    for gate, body in stubs.items():
        script = tmp / "scripts" / "ci" / gate
        script.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho \"$0\" >> \"$CALL_LOG\"\n" + body)
        script.chmod(0o755)
    return tmp


def run(
    script: Path, *args: str, cwd: Path, env: dict[str, str] | None = None, timeout: int = 180
) -> subprocess.CompletedProcess:
    environ = dict(os.environ, CALL_LOG=str(cwd / "gate-calls.log"), PYTHON=sys.executable, ACT_MCP_TIMEOUT="5")
    environ.update(env or {})
    return subprocess.run(
        [str(script), *args], cwd=str(cwd), capture_output=True, text=True, env=environ, timeout=timeout
    )


class WorkspaceResolutionTest(unittest.TestCase):
    """The bug this script fixes is a path bug, so the path it picks is the first thing tested."""

    def test_resolves_its_own_tree_from_a_foreign_directory(self) -> None:
        result = run(POST_COMMIT, "--dry-run", cwd=Path("/tmp"), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"workspace: {REPO} (resolved from", result.stdout)

    def test_workspace_argument_wins_over_the_scripts_own_location(self) -> None:
        tmp = make_repo(Path(tempfile.mkdtemp()))
        result = run(POST_COMMIT, "--dry-run", "--workspace", str(tmp), cwd=Path("/tmp"), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"workspace: {tmp} (resolved from --workspace)", result.stdout)

    def test_a_nested_run_of_the_live_suites_own_root_is_refused(self) -> None:
        """The cycle is coverage.sh -> this suite -> post_commit.sh -> coverage.sh.

        ``scripts/ci/coverage.sh`` exports ``FITBIT_CI_SUITE`` as the root whose suite is
        live, so a nested run that resolves to that same root is the fork bomb, and it is
        refused with a ``fix:`` line before any gate runs. A stub repository - which is
        what a test actually points at - is a different root and stays allowed.
        """
        tmp = make_repo(Path(tempfile.mkdtemp()))
        nested = run(POST_COMMIT, "--via", "direct", cwd=tmp, env={"FITBIT_CI_SUITE": str(tmp)})
        self.assertEqual(nested.returncode, 2, nested.stdout + nested.stderr)
        self.assertIn("already running", nested.stderr)
        self.assertIn("fix:", nested.stderr)
        self.assertNotIn("stub coverage", nested.stdout, "the refusal comes before any gate runs")

    def test_a_dry_run_of_the_live_suites_own_root_is_still_allowed(self) -> None:
        """A dry run starts no gate, so it cannot be the recursion the guard stops."""
        result = run(POST_COMMIT, "--dry-run", cwd=Path("/tmp"), env={"FITBIT_CI_SUITE": str(REPO)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"workspace: {REPO} (resolved from", result.stdout)

    def test_the_marker_allows_a_run_against_any_other_root(self) -> None:
        tmp = make_repo(Path(tempfile.mkdtemp()))
        result = run(POST_COMMIT, "--via", "direct", cwd=tmp, env={"FITBIT_CI_SUITE": str(REPO)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stub coverage", result.stdout)

    def test_a_tree_with_no_workflows_says_so_and_names_the_paths_tried(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        script = tmp / "scripts" / "ci"
        script.mkdir(parents=True)
        shutil.copy(POST_COMMIT, script / "post_commit.sh")
        result = run(script / "post_commit.sh", "--dry-run", cwd=tmp, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("fix:", result.stderr)
        self.assertIn(".github/workflows", result.stderr)
        self.assertIn(str(tmp), result.stderr, "the path it tried is named in the message")

    def test_a_bad_explicit_workspace_is_refused_with_the_path_named(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "does-not-exist"
        result = run(POST_COMMIT, "--dry-run", "--workspace", str(missing), cwd=Path("/tmp"), timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("fix:", result.stderr)
        self.assertIn(str(missing), result.stderr)

    def test_dry_run_names_both_required_jobs_and_runs_nothing(self) -> None:
        result = run(POST_COMMIT, "--dry-run", cwd=Path("/tmp"), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("coverage: ./scripts/ci/coverage.sh --no-install", result.stdout)
        self.assertIn("benchmarks: ./scripts/ci/bench.sh --no-install", result.stdout)
        self.assertNotIn("Ran ", result.stdout, "a dry run must not run a gate")

    def test_quick_skips_the_benchmark(self) -> None:
        tmp = make_repo(Path(tempfile.mkdtemp()))
        result = run(POST_COMMIT, "--quick", "--via", "direct", cwd=tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"benchmarks\s+SKIP")
        self.assertNotIn("stub bench", result.stdout)


class GateSummaryTest(unittest.TestCase):
    """scripts/ci/_gate_summary.py, fed reports it did not produce."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def coverage_json(self, covered: int, name: str = "coverage.json") -> Path:
        path = self.tmp / name
        path.write_text(
            json.dumps(
                {
                    "totals": {
                        "num_statements": 100,
                        "covered_lines": covered,
                        "num_branches": 0,
                        "covered_branches": 0,
                    },
                    "files": {},
                }
            )
        )
        return path

    def pyproject(self, floor: float = 85.0) -> Path:
        path = self.tmp / "pyproject.toml"
        path.write_text(f"[tool.coverage.report]\nfail_under = {floor}\n")
        return path

    def bench_json(self, regressed: list[str] | None = None, *, gated: bool = True, name: str = "bench-results.json") -> Path:
        path = self.tmp / name
        path.write_text(
            json.dumps(
                {
                    "cases": [
                        {"case": "seed_corpus", "tracked": True, "regression_percent": 2.0},
                        {"case": "scan_intraday", "tracked": True, "regression_percent": 31.0},
                    ],
                    "gate": {
                        "gated": gated,
                        "passed": not regressed,
                        "tracked_cases": 2,
                        "regressed_cases": regressed or [],
                        "tolerance_percent": 15.0,
                        "skipped_reason": None if gated else "report only (--no-gate)",
                    },
                }
            )
        )
        return path

    # --- the verdicts ---------------------------------------------------------
    def test_coverage_percentage_and_committed_floor_are_both_reported(self) -> None:
        verdict = summary.coverage_verdict(self.coverage_json(92), None, self.pyproject())
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.detail, "92.0% covered (floor 85.0%)")

    def test_coverage_below_its_floor_is_a_failing_verdict(self) -> None:
        verdict = summary.coverage_verdict(self.coverage_json(80), None, self.pyproject())
        self.assertFalse(verdict.ok)
        self.assertIn("BELOW FLOOR", verdict.detail)

    def test_coverage_is_read_from_the_log_when_no_report_survived_the_container(self) -> None:
        log = self.tmp / "coverage.log"
        log.write_text(COVERAGE_JOB_LOG)
        verdict = summary.coverage_verdict(None, log, self.pyproject())
        self.assertTrue(verdict.ok)
        self.assertIn("92.0% covered", verdict.detail)
        self.assertEqual(verdict.source, "job log")

    def test_a_missing_report_and_missing_log_is_admitted_not_invented(self) -> None:
        verdict = summary.coverage_verdict(self.tmp / "none.json", self.tmp / "none.log", self.pyproject())
        self.assertIsNone(verdict.ok)
        self.assertEqual(verdict.detail, "no coverage number reported")

    def test_bench_verdict_from_the_results_document(self) -> None:
        verdict = summary.bench_verdict(self.bench_json(), None, None)
        self.assertTrue(verdict.ok)
        self.assertIn("no case regressed more than 15%", verdict.detail)
        self.assertIn("2 tracked case(s)", verdict.detail)

    def test_bench_regressed_cases_are_named(self) -> None:
        verdict = summary.bench_verdict(self.bench_json(["scan_intraday"]), None, None)
        self.assertFalse(verdict.ok)
        self.assertIn("scan_intraday", verdict.detail)

    def test_an_ungated_benchmark_run_is_not_reported_as_a_pass(self) -> None:
        verdict = summary.bench_verdict(self.bench_json(gated=False), None, None)
        self.assertIsNone(verdict.ok)
        self.assertIn("not gated", verdict.detail)

    def test_bench_verdict_falls_back_to_the_markdown_then_the_log(self) -> None:
        markdown = self.tmp / "bench-summary.md"
        markdown.write_text(
            "## Benchmarks\n\n**PASS** - no case regressed more than 15% against the committed baseline\n"
        )
        verdict = summary.bench_verdict(None, markdown, None)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.source, "bench-summary.md")
        log = self.tmp / "benchmark.log"
        log.write_text(BENCH_JOB_LOG)
        verdict = summary.bench_verdict(None, None, log)
        self.assertTrue(verdict.ok)
        self.assertIn("no case regressed more than 15%", verdict.detail)

    # --- the block ------------------------------------------------------------
    def run_summary(self, *args: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = summary.main(list(args))
        return code, buffer.getvalue()

    def test_a_value_that_begins_with_a_dash_is_still_a_value(self) -> None:
        """One way the runner can resolve the workspace is the literal ``--workspace``."""
        code, out = self.run_summary(
            "--workspace",
            str(self.tmp),
            "--workspace-source",
            "--workspace",
            "--gate",
            "coverage=PASS",
            "--coverage-json",
            str(self.coverage_json(92)),
        )
        self.assertEqual(code, 0, out)
        self.assertIn("(resolved from --workspace)", out)

    def test_the_block_carries_the_coverage_number_the_bench_verdict_and_one_result(self) -> None:
        code, out = self.run_summary(
            "--workspace",
            str(self.tmp),
            "--workspace-source",
            "--workspace",
            "--runner",
            "direct",
            "--gate",
            "lint=PASS",
            "--gate",
            "coverage=PASS",
            "--gate",
            "benchmarks=PASS",
            "--coverage-json",
            str(self.coverage_json(92)),
            "--bench-json",
            str(self.bench_json()),
        )
        self.assertEqual(code, 0)
        self.assertIn(str(self.tmp), out)
        self.assertRegex(out, r"coverage\s+PASS\s+92\.0% covered \(floor 85\.0%\)")
        self.assertRegex(out, r"benchmarks\s+PASS\s+no case regressed more than 15%")
        self.assertRegex(out, r"result\s+PASS\s+3 passed, 0 failed")

    def test_a_passing_process_with_a_failing_report_is_a_fail(self) -> None:
        """The runner says the job exited 0; the report says the floor was missed. The report wins."""
        code, out = self.run_summary(
            "--gate",
            "lint=PASS",
            "--gate",
            "coverage=PASS",
            "--gate",
            "benchmarks=PASS",
            "--coverage-json",
            str(self.coverage_json(70)),
            "--bench-json",
            str(self.bench_json()),
        )
        self.assertEqual(code, 1)
        self.assertRegex(out, r"coverage\s+FAIL\s+70\.0% covered \(floor 85\.0%\) BELOW FLOOR")

    def test_a_regressed_case_fails_the_block(self) -> None:
        code, out = self.run_summary(
            "--gate",
            "coverage=PASS",
            "--gate",
            "benchmarks=PASS",
            "--coverage-json",
            str(self.coverage_json(92)),
            "--bench-json",
            str(self.bench_json(["scan_intraday"])),
        )
        self.assertEqual(code, 1)
        self.assertRegex(out, r"benchmarks\s+FAIL\s+1 case\(s\) regressed more than 15%: scan_intraday")

    def test_a_skipped_gate_does_not_make_the_run_red(self) -> None:
        code, out = self.run_summary(
            "--gate",
            "coverage=PASS",
            "--coverage-json",
            str(self.coverage_json(92)),
            "--gate",
            "benchmarks=SKIP",
        )
        self.assertEqual(code, 0)
        self.assertRegex(out, r"result\s+PASS\s+1 passed, 0 failed, 1 skipped")


class ActMcpClientTest(unittest.TestCase):
    """scripts/ci/_act_mcp.py against a stub server: the arguments that arrive are the point."""

    def setUp(self) -> None:
        self.tmp = make_repo(Path(tempfile.mkdtemp()))

    def act(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ACT_MCP), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ, ACT_MCP_TIMEOUT="5"),
        )

    def test_plan_prints_the_workspace_without_touching_the_server(self) -> None:
        result = self.act(
            "--config",
            "/nonexistent/mcp.json",
            "plan",
            "--workspace",
            str(self.tmp),
            "--job",
            "coverage",
            "--workflow",
            "ci.yml",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["arguments"]["workspace"], str(self.tmp))
        self.assertEqual(plan["arguments"]["job"], "coverage")
        self.assertEqual(plan["arguments"]["workflow"], "ci.yml")

    def test_plan_refuses_a_directory_with_no_workflows(self) -> None:
        empty = Path(tempfile.mkdtemp())
        result = self.act("--config", str(empty / "mcp.json"), "plan", "--workspace", str(empty))
        self.assertEqual(result.returncode, 2)
        self.assertIn("fix:", result.stderr)
        self.assertIn(".github", result.stderr)
        self.assertIn(str(empty), result.stderr)

    def test_run_passes_the_repository_root_as_the_workspace_argument(self) -> None:
        with StubMcpServer([RUN_WORKFLOW_TOOL], job_output=COVERAGE_JOB_LOG) as server:
            result = self.act(
                "--url",
                server.url,
                "run",
                "--workspace",
                str(self.tmp),
                "--workflow",
                "ci.yml",
                "--job",
                "coverage",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("coverage: 92.0%", result.stdout)
            self.assertEqual(len(server.server.calls), 1)
            arguments = server.server.calls[0]["arguments"]
            self.assertEqual(arguments["workspace"], str(self.tmp))
            self.assertEqual(arguments["job"], "coverage")
            self.assertEqual(arguments["workflow"], "ci.yml")
            self.assertIn("stub-session", server.server.sessions, "the session id came back on later calls")

    def test_a_server_that_answers_in_sse_is_understood(self) -> None:
        with StubMcpServer([RUN_WORKFLOW_TOOL], job_output=BENCH_JOB_LOG, sse=True) as server:
            result = self.act("--url", server.url, "run", "--workspace", str(self.tmp), "--job", "benchmark")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no regressions beyond 15%", result.stdout)

    def test_a_failing_job_is_reported_as_a_failure_not_a_success(self) -> None:
        failing = "coverage floor failed: overall 81.0% is below the floor 85.0%"
        with StubMcpServer([RUN_WORKFLOW_TOOL], job_output=failing) as server:
            result = self.act("--url", server.url, "run", "--workspace", str(self.tmp), "--job", "coverage")
        self.assertEqual(result.returncode, 1)
        with StubMcpServer([RUN_WORKFLOW_TOOL], job_output="the job ran and said no") as server:
            server.server.job_is_error = True
            result = self.act("--url", server.url, "run", "--workspace", str(self.tmp), "--job", "coverage")
        self.assertEqual(result.returncode, 1)

    def test_a_tool_with_no_workspace_argument_refuses_to_guess(self) -> None:
        with StubMcpServer([NO_WORKSPACE_TOOL]) as server:
            result = self.act("--url", server.url, "discover")
            self.assertEqual(result.returncode, 2)
            self.assertIn("fix:", result.stderr)
            self.assertIn("no workspace argument", result.stderr)

    def test_config_picks_the_act_mcp_entry_among_several(self) -> None:
        config = Path(tempfile.mkdtemp()) / "mcp.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "other": {"url": "http://127.0.0.1:1/mcp"},
                        "act-mcp": {"url": "http://127.0.0.1:2/mcp", "timeout": 1900},
                    }
                }
            )
        )
        name, url, timeout = act_mcp.pick_server(act_mcp.read_mcp_config(config), None)
        self.assertEqual(name, "act-mcp")
        self.assertEqual(url, "http://127.0.0.1:2/mcp")
        self.assertEqual(timeout, 1900.0)

    def test_a_missing_config_explains_how_to_add_one(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "mcp.json"
        result = self.act("--config", str(missing), "discover")
        self.assertEqual(result.returncode, 2)
        self.assertIn("fix:", result.stderr)
        self.assertIn(str(missing), result.stderr)

    def test_an_unreachable_server_is_exit_2_so_the_caller_falls_back(self) -> None:
        result = self.act("--url", "http://127.0.0.1:1/mcp", "--timeout", "2", "discover")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot reach", result.stderr)


class PostCommitRunTest(unittest.TestCase):
    """The runner end to end, against stub gates and a stub MCP server."""

    def setUp(self) -> None:
        self.tmp = make_repo(Path(tempfile.mkdtemp()))

    def test_direct_mode_runs_both_gates_and_prints_the_combined_block(self) -> None:
        result = run(POST_COMMIT, "--via", "direct", cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"workspace: {self.tmp} (resolved from", result.stdout)
        self.assertIn("runner:    direct", result.stdout)
        self.assertIn("stub coverage", result.stdout)
        self.assertIn("stub bench", result.stdout)
        self.assertRegex(result.stdout, r"coverage\s+PASS\s+92\.0% covered \(floor 85\.0%\)")
        self.assertRegex(result.stdout, r"benchmarks\s+PASS\s+no case regressed more than 15%")
        self.assertRegex(result.stdout, r"result\s+PASS\s+3 passed, 0 failed")
        calls = (self.tmp / "gate-calls.log").read_text()
        for expected in ("lint.sh", "coverage.sh", "bench.sh"):
            self.assertIn(expected, calls)

    def test_a_failing_gate_fails_the_run(self) -> None:
        gate = self.tmp / "scripts" / "ci" / "bench.sh"
        gate.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho \"$0\" >> \"$CALL_LOG\"\nexit 1\n")
        gate.chmod(0o755)
        result = run(POST_COMMIT, "--via", "direct", cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"benchmarks\s+FAIL")
        self.assertRegex(result.stdout, r"result\s+FAIL")

    def test_act_mcp_mode_passes_the_resolved_root_and_reports_from_the_job_log(self) -> None:
        output = COVERAGE_JOB_LOG + "\n" + BENCH_JOB_LOG
        with StubMcpServer([RUN_WORKFLOW_TOOL], job_output=output) as server:
            result = run(POST_COMMIT, cwd=self.tmp, env={"ACT_MCP_URL": server.url})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"workspace: {self.tmp}", result.stdout)
        jobs = [call["arguments"].get("job") for call in server.server.calls]
        self.assertEqual(jobs, ["lint", "coverage", "benchmark"])
        for call in server.server.calls:
            self.assertEqual(call["arguments"]["workspace"], str(self.tmp))
        self.assertRegex(result.stdout, r"coverage\s+PASS\s+92\.0% covered")
        self.assertRegex(result.stdout, r"benchmarks\s+PASS\s+no case regressed more than 15%")
        self.assertRegex(result.stdout, r"result\s+PASS\s+3 passed, 0 failed")

    def test_an_unreachable_server_falls_back_to_the_gate_scripts(self) -> None:
        result = run(POST_COMMIT, cwd=self.tmp, env={"ACT_MCP_URL": "http://127.0.0.1:1/mcp"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("act-mcp is not usable here", result.stdout)
        self.assertIn("runner:    direct", result.stdout)
        self.assertRegex(result.stdout, r"result\s+PASS")

    def test_a_job_the_runner_cannot_run_falls_back_instead_of_passing_blindly(self) -> None:
        with StubMcpServer([RUN_WORKFLOW_TOOL], job_output="the job did not run") as server:
            server.server.job_is_error = True
            result = run(POST_COMMIT, cwd=self.tmp, env={"ACT_MCP_URL": server.url})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("in this checkout instead", result.stdout)
        self.assertIn("coverage.sh", (self.tmp / "gate-calls.log").read_text())

    def test_no_workspace_means_no_run_and_a_fix_line(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        script = tmp / "scripts" / "ci"
        script.mkdir(parents=True)
        shutil.copy(POST_COMMIT, script / "post_commit.sh")
        result = run(script / "post_commit.sh", cwd=tmp, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("fix:", result.stderr)


class GitHookTest(unittest.TestCase):
    """The opt-in hook: it finds the runner through its own symlink and never eats a commit."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = make_repo(self.tmp / "repo")
        subprocess.run(["git", "init", "-q"], cwd=str(self.root), capture_output=True, text=True, check=True)
        (self.root / "scripts" / "git-hooks").mkdir(parents=True)
        for source in (HOOK, HOOK_INSTALLER):
            shutil.copy(source, self.root / "scripts" / "git-hooks" / source.name)
        self.installer = self.root / "scripts" / "git-hooks" / "install.sh"
        self.hook = self.root / ".git" / "hooks" / "post-commit"

    def hook_env(self, **extra: str) -> dict[str, str]:
        return dict(
            os.environ,
            PYTHON=sys.executable,
            CALL_LOG=str(self.tmp / "calls.log"),
            ACT_MCP_URL="http://127.0.0.1:1/mcp",
            **extra,
        )

    def install(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.installer), *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_installer_links_the_hook_in_and_removes_it_again(self) -> None:
        installed = self.install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertIn("installed post-commit", installed.stdout)
        self.assertTrue(self.hook.is_symlink())
        self.assertEqual(Path(self.hook).readlink(), self.root / "scripts" / "git-hooks" / "post-commit")
        again = self.install("--status")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("post-commit ->", again.stdout)
        self.assertEqual(self.install().returncode, 0)  # idempotent
        removed = self.install("--remove")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.hook.exists())

    def test_installer_refuses_to_clobber_someone_elses_hook(self) -> None:
        self.hook.unlink(missing_ok=True)
        self.hook.write_text("#!/usr/bin/env bash\necho not yours\n")
        self.hook.chmod(0o755)
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("refused", result.stderr)
        self.assertIn("--force", result.stderr)
        self.assertIn("not yours", self.hook.read_text())

    def test_hook_runs_the_runner_against_the_repository_that_holds_it(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        result = subprocess.run(
            [str(self.hook)], cwd=str(self.root), capture_output=True, text=True, env=self.hook_env(), timeout=180
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"workspace: {self.root}", result.stdout)
        self.assertRegex(result.stdout, r"result\s+PASS")
        self.assertIn("coverage.sh", (self.tmp / "calls.log").read_text())

    def test_hook_reports_a_failed_gate_and_still_exits_zero(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        gate = self.root / "scripts" / "ci" / "coverage.sh"
        gate.write_text("#!/usr/bin/env bash\necho 'stub coverage failed' >&2\nexit 1\n")
        gate.chmod(0o755)
        result = subprocess.run(
            [str(self.hook)], cwd=str(self.root), capture_output=True, text=True, env=self.hook_env(), timeout=180
        )
        self.assertEqual(result.returncode, 0, "a gate failure must not undo a recorded commit")
        self.assertIn("a gate FAILED", result.stderr)
        strict = subprocess.run(
            [str(self.hook)],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=self.hook_env(POST_COMMIT_STRICT="1"),
            timeout=180,
        )
        self.assertNotEqual(strict.returncode, 0)

    def test_hook_can_be_skipped_once(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        result = subprocess.run(
            [str(self.hook)],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=self.hook_env(POST_COMMIT_SKIP="1"),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("gates skipped", result.stdout)


class DocumentedTest(unittest.TestCase):
    """The ticket asks for documentation; a doc that does not match the script is worse than none."""

    def setUp(self) -> None:
        self.doc = (REPO / "docs" / "ci.md").read_text()

    def test_ci_doc_names_the_act_mcp_invocation_and_the_act_safe_workflows(self) -> None:
        self.assertIn("act-mcp", self.doc)
        self.assertIn(".pi/mcp.json", self.doc)
        self.assertIn("scripts/ci/post_commit.sh", self.doc)
        self.assertIn("scripts/git-hooks/install.sh", self.doc)
        for workflow in ("ci.yml", "bench.yml"):
            self.assertIn(workflow, self.doc)
        self.assertRegex(self.doc.lower(), r"act-safe")

    def test_ci_doc_names_the_two_helpers_it_tells_you_to_run(self) -> None:
        self.assertIn("scripts/ci/_act_mcp.py", self.doc)
        self.assertIn("scripts/ci/_gate_summary.py", self.doc)

    def test_ci_doc_shows_the_exact_invocation_and_the_workflow_job_names(self) -> None:
        self.assertIn("_act_mcp.py run --workspace", self.doc)
        self.assertIn("-j coverage", self.doc)
        self.assertIn("-j benchmark", self.doc)

    def test_readme_points_at_the_hook_installer(self) -> None:
        readme = (REPO / "README.md").read_text()
        self.assertIn("scripts/git-hooks", readme)

    def test_no_hook_is_installed_by_default_and_the_scripts_are_executable(self) -> None:
        for path in (HOOK, HOOK_INSTALLER, POST_COMMIT):
            self.assertTrue(os.access(path, os.X_OK), f"{path} is not executable")
        self.assertTrue(re.search(r"opt-in", HOOK.read_text()), "the hook must say it is opt-in")


if __name__ == "__main__":
    unittest.main()
