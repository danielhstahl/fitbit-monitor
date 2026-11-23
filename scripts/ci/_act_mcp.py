#!/usr/bin/env python3
"""Run one GitHub Actions job through the ``act-mcp`` MCP server, workspace included.

Why this file exists
--------------------
``act-mcp`` is configured for this workspace (``.pi/mcp.json``), and its previous run
here ended with::

    act-mcp: workspace /Users/.../fitbit-monitor has no `.github` directory, so there
    are no GitHub Actions workflows to run
      fix: point --workspace at the repository root that contains .github/workflows/*.yml

That failure is a *path* failure, not a workflow failure: the caller handed the server
whatever directory it was standing in. So this client does one job and does it
loudly - resolve the workspace on the command line, print it before the call, and
refuse to pretend a run happened when the server could not be reached::

    workspace: /abs/repo (from --workspace)
    act-mcp:   http://host.docker.internal:51349/mcp  tool=run_workflow args={"workspace": ..., "job": ...}

Usage (``scripts/ci/post_commit.sh`` drives this; the subcommands are also useful by hand)::

    scripts/ci/_act_mcp.py discover                       # what tool and args would be used
    scripts/ci/_act_mcp.py plan --job coverage --workflow ci.yml
    scripts/ci/_act_mcp.py run --job coverage --workflow ci.yml --workspace /abs/repo
    scripts/ci/_act_mcp.py run --job benchmark --workflow bench.yml --log /tmp/bench.log

Exit codes::

    0  the tool ran and reported success
    1  the tool ran and reported failure (the job is red - that is a real verdict)
    2  act-mcp could not be used (no config, no route, no usable tool, a required
       argument with no value, a server pointed at somebody else's tree) - the caller
       falls back to running the gate directly

Two honesty rules. The workspace is never guessed at: ``--workspace`` is required, is
made absolute, and is checked for ``.github/workflows`` before anything is sent. And
the argument *names* the server wants are read out of the tool's own ``inputSchema``
rather than hardcoded, because the server is a separate binary whose parameter names
this repository does not control.

Which leaves the case the schema cannot answer: a server that *binds* its workspace at
launch and therefore has no workspace argument at all - act-mcp 0.0.4 is exactly that::

    inputSchema properties: category, commit, idempotency_key, inputs, log_tail_bytes,
                            secrets, task, timeout_secs, wait, workflow

Dropping the workspace there is not a guess, it is a different tree, so such a server is
*asked* which tree it owns (the ``instructions`` of its own ``initialize`` answer names
it) and the call is only made when that is the tree being gated here - see
:func:`plan_workspace`. A server that owns another checkout exits ``2`` with both paths
named rather than reporting someone else's build green. The remaining arguments the
bound server insists on (``task``, ``category``, ``commit``, ``wait``) are derived -
job id, category hint from ``list_tasks``, ``git rev-parse HEAD``, and "wait for the
terminal answer" - and the whole argument set is printed before it is sent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "fitbit-monitor-post-commit"

#: Server keys to try, in order, when no ``--server`` was given.
SERVER_PREFERENCES = ("act-mcp", "act", "actions", "workflows")

#: Argument names that mean "run this repository", best first. Matched case-insensitively
#: with punctuation removed, so ``workingDirectory`` and ``working_directory`` both hit.
WORKSPACE_ARGS = (
    "workspace",
    "workingdirectory",
    "workdir",
    "repositoryroot",
    "repofolder",
    "directory",
    "repository",
    "repo",
    "projectroot",
    "root",
    "path",
    "cwd",
)

#: Argument names that mean "run this job" / "this workflow file", best first.
#: ``task`` leads because act-mcp calls the job id that (it is "exactly what ``act -j``
#: takes"), and a server offering both means act-mcp.
JOB_ARGS = ("task", "job", "jobid", "jobname", "job_id", "job_name")
WORKFLOW_ARGS = ("workflow", "workflowfile", "workflow_file", "workflowpath", "file", "name")

#: Optional arguments whose value this client can work out for itself, matched the same
#: normalised way. Each has a reason to be sent: the category is what makes act-mcp write
#: an evidence file for the run, the commit ties the run to the checkout being gated,
#: ``wait`` is what makes one call produce a verdict, and the log tail is what
#: ``scripts/ci/_gate_summary.py`` reads a number out of when the reports stayed in the
#: container. Only names the server actually declares are sent.
CATEGORY_ARGS = ("category",)
COMMIT_ARGS = ("commit", "commitish", "rev", "sha", "ref")
WAIT_ARGS = ("wait",)
RUN_TIMEOUT_ARGS = ("timeout_secs", "timeout_seconds", "timeout")
LOG_TAIL_ARGS = ("log_tail_bytes", "log_tail")

#: A read-only catalogue of what the server can run, and the per-job category hint.
LIST_TASKS_TOOL = r"list[_-]?tasks"

#: How much of the job log to ask for. act-mcp 0.0.4 refuses more than 64 KiB, and a
#: coverage run's summary lines are the last thing it prints, so the ceiling is the right
#: amount to ask for.
LOG_TAIL_BYTES = 65_536

#: Run statuses that mean the job did not pass. act-mcp answers a failed workflow as a
#: normal tool result (`isError` false, `status: "failed"`), so reading only `isError`
#: would report a red job green.
FAILED_STATUSES = ("failed", "timed_out", "timeout", "error", "cancelled", "aborted")

#: A tool that runs a workflow, best first.
TOOL_PREFERENCE = (
    r"^run[_-]?workflow$",
    r"run[_-]?workflow",
    r"workflow",
    r"\bact\b",
    r"run[_-]?job",
    r"trigger",
)

UNAVAILABLE = 2


class ActMcpError(Exception):
    """Anything that stops us asking the server politely - message is the ``fix:`` line."""


# --- configuration ------------------------------------------------------------
def read_mcp_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ActMcpError(
            f"{path} does not exist, so no act-mcp server is configured here\n"
            f"  fix: add the act-mcp server to {path} "
            '({"mcpServers": {"act-mcp": {"url": "http://host:port/mcp"}}}), '
            "or run the gates directly with --via direct"
        )
    try:
        raw = json.loads(path.read_text())
    except ValueError as exc:
        raise ActMcpError(f"{path} is not readable JSON ({exc})\n  fix: repair the mcpServers block") from exc
    servers = raw.get("mcpServers") or {}
    if not isinstance(servers, dict) or not servers:
        raise ActMcpError(f"{path} declares no mcpServers\n  fix: add the act-mcp server entry")
    return servers


def pick_server(servers: dict[str, Any], wanted: str | None) -> tuple[str, str, float | None]:
    """(name, url, timeout-seconds) for the act-mcp entry, or an ActMcpError naming what was configured.

    The optional ``timeout`` in an mcp config is per-request seconds (the same unit the
    harness that reads this file uses), so it is passed through unchanged.
    """
    if wanted:
        if wanted not in servers:
            raise ActMcpError(
                f"no server named {wanted!r} in mcpServers (configured: {', '.join(sorted(servers))})\n"
                "  fix: pass --server one of those names, or add the act-mcp entry"
            )
        entry = servers[wanted]
    else:
        entry = None
        wanted = ""
        for preference in SERVER_PREFERENCES:
            for name in sorted(servers):
                if name.lower() == preference:
                    entry, wanted = servers[name], name
                    break
            if entry is not None:
                break
        if entry is None:
            for name in sorted(servers):
                if any(preference in name.lower() for preference in SERVER_PREFERENCES):
                    entry, wanted = servers[name], name
                    break
        if entry is None:
            raise ActMcpError(
                f"no act-mcp server in mcpServers (configured: {', '.join(sorted(servers))})\n"
                "  fix: add one named act-mcp, or pass --server NAME"
            )
    url = entry.get("url") if isinstance(entry, dict) else None
    if not url or not str(url).startswith(("http://", "https://")):
        raise ActMcpError(
            f"server {wanted!r} has no http(s) url in the mcp config\n"
            "  fix: give it a Streamable-HTTP url, e.g. http://host.docker.internal:51349/mcp"
        )
    timeout: float | None = None
    if isinstance(entry, dict) and entry.get("timeout") is not None:
        try:
            timeout = float(entry["timeout"])
        except (TypeError, ValueError):
            timeout = None
    return wanted, str(url), timeout


# --- a server that owns its own workspace --------------------------------------
#: How a workspace-bound server says which tree it runs. act-mcp 0.0.4 puts it in the
#: prose of its ``instructions``: ``... Workspace /abs/fitbit-monitor; evidence root
#: /abs/fitbit-monitor/.evidence ...``. The separator class is deliberately narrow: it
#: allows ``Workspace /p``, ``workspace: /p`` and ``workspace=/p``, and nothing else, so
#: the sentence "One run at a time per workspace and there is no queue" is not mistaken
#: for a path.
WORKSPACE_ADVERTISEMENT = re.compile(r"workspace\b[:=]?\s*(/[^\s,;]+)", re.IGNORECASE)


def advertised_workspace(init_result: dict[str, Any]) -> Path | None:
    """The tree a workspace-bound server owns, in its own words - never assumed.

    Read from a machine-readable ``workspace`` field if a future server grows one, else
    out of the ``instructions`` text. ``None`` means the server never said, which is a
    different (and worse) answer than "it said somewhere else".
    """
    named = init_result.get("workspace") if isinstance(init_result, dict) else None
    if isinstance(named, str) and named.strip():
        return Path(named.strip()).expanduser().resolve()
    match = WORKSPACE_ADVERTISEMENT.search(str(init_result.get("instructions") or ""))
    return Path(match.group(1)).expanduser().resolve() if match else None


class WorkspaceMode(NamedTuple):
    """How the repository root reaches the server, and one line saying so."""

    argument: str | None
    note: str


def plan_workspace(
    *,
    tool: dict[str, Any],
    client: McpHttp,
    requested: Path,
    override: str | None,
    server_bound: bool,
) -> WorkspaceMode:
    """Which argument carries the root - or why none does, and why that is the same tree.

    Three answers, in this order: an explicit ``--workspace-arg`` (an instruction, used
    whatever the schema says); a workspace-shaped argument in the tool's own schema (the
    normal case, and the only one that lets one server gate many checkouts); and a
    workspace-bound server, which is only believed when it names the tree being gated.
    """
    if override:
        return WorkspaceMode(override, f"sent as --{override}, per the caller's override")
    key = pick_argument(tool, WORKSPACE_ARGS, "workspace", optional=True)
    if key:
        return WorkspaceMode(key, f"sent as {key}={requested}")
    advertised = advertised_workspace(client.init_result)
    if advertised == requested:
        return WorkspaceMode(None, f"the server binds its own workspace and it is this one ({requested})")
    if server_bound:
        return WorkspaceMode(
            None,
            f"the server binds its own workspace{f' ({advertised})' if advertised else ''} and "
            "--server-bound was passed: the run measures that checkout, not this one",
        )
    raise ActMcpError(
        f"the act-mcp tool {tool.get('name')!r} has no workspace argument, and the server runs "
        f"{advertised if advertised else 'a workspace it never named'} rather than {requested}\n"
        f"  fix: start act-mcp against this checkout (act-mcp --workspace {requested}), or pass "
        "--server-bound to accept a run in the server's own tree, or run the gates directly "
        "with --via direct"
    )


def head_commit(workspace: Path) -> str | None:
    """The commit a server that resolves commits locally should be told to run."""
    try:
        done = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{7,40}", sha) else None


def list_tasks_tool(tools: list[dict[str, Any]]) -> str | None:
    """The read-only catalogue tool, if this server has one (act-mcp does)."""
    for tool in tools:
        name = str(tool.get("name") or "") if isinstance(tool, dict) else ""
        if name and re.search(LIST_TASKS_TOOL, name.lower()):
            return name
    return None


def category_hint(client: McpHttp, tool_name: str, *, workflow: str | None, job: str | None) -> str | None:
    """The server's own hint about what kind of job this is (coverage / benchmark / other).

    ``list_tasks`` is documented as advisory and read-only, so a failure here is not a
    failure of the run: it returns ``None`` and the caller falls back to reading the hint
    out of the job's name. Getting it right is worth the call, because the category is
    what decides whether act-mcp writes an evidence file for the run at all.
    """
    try:
        result = client.call_tool(tool_name, {"workflow": workflow} if workflow else {})
    except ActMcpError:
        return None
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        data = _json_from_text(result_text(result))
    wanted = _workflow_names(workflow) if workflow else set()
    entries = (data.get("workflows") or []) if isinstance(data, dict) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if wanted:
            spelled = _workflow_names(str(entry.get("name") or "")) | _workflow_names(str(entry.get("file") or ""))
            if not wanted & spelled:
                continue
        for task in entry.get("jobs") or []:
            if isinstance(task, dict) and (job is None or str(task.get("id")) == job):
                hint = task.get("category_hint")
                if isinstance(hint, str) and hint.strip():
                    return hint.strip()
    return None


def _workflow_names(value: str) -> set[str]:
    """Every way one workflow file gets spelled: ``ci``, ``ci.yml``, the path, the file."""
    stem = Path(value).name
    return {name for name in (stem, Path(stem).stem, value, value.strip("/")) if name}


def resolve_workspace(value: str) -> Path:
    """Absolute path that really holds ``.github/workflows``, or an ActMcpError saying so."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    workflows = path / ".github" / "workflows"
    if not workflows.is_dir():
        raise ActMcpError(
            f"{path} has no .github directory, so there are no GitHub Actions workflows to run\n"
            f"  fix: point --workspace at the repository root that contains {workflows}/*.yml "
            "(git rev-parse --show-toplevel prints it)"
        )
    return path


# --- the client ---------------------------------------------------------------
class McpHttp:
    """Just enough of the MCP Streamable-HTTP transport to list and call one tool.

    One POST per JSON-RPC message, ``Accept`` advertising both shapes the spec allows,
    and the session id (if the server issues one) echoed back on every later call.
    Responses are read whether they arrive as ``application/json`` or as an
    ``text/event-stream`` body with the answer on a ``data:`` line.
    """

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self.protocol_version = PROTOCOL_VERSION
        self.server_info: dict[str, Any] = {}
        self.init_result: dict[str, Any] = {}
        #: filled by connect(): the whole catalogue, so a caller can find list_tasks too
        self.tools: list[dict[str, Any]] = []

    def _request(self, payload: dict[str, Any] | None, *, expect: bool) -> dict[str, Any] | None:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": f"{CLIENT_NAME}/1.0",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
                content_type = response.headers.get("Content-Type", "")
                issued = response.headers.get("Mcp-Session-Id")
                if issued:
                    self.session_id = issued
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise ActMcpError(f"act-mcp answered HTTP {exc.code} for {self.url}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ActMcpError(f"cannot reach the act-mcp server at {self.url}: {exc}") from exc
        if payload is not None and payload.get("id") is None:
            return None  # a notification: a 202 with no body is the expected answer
        if not expect:
            return None
        return self._decode(body, content_type, payload)

    @staticmethod
    def _decode(body: str, content_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        wanted = (payload or {}).get("id")
        candidates: list[dict[str, Any]] = []
        # Streamable HTTP allows the answer either as a JSON document or as server-sent
        # events with the message on a `data:` line - and servers do not all label the one
        # they sent, so the shape is read off the body rather than off the Content-Type.
        chunks = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        chunks = [chunk for chunk in chunks if chunk and chunk != "[DONE]"]
        if not chunks and body.strip():
            chunks = [body.strip()]
        for chunk in chunks:
            try:
                parsed = json.loads(chunk)
            except ValueError:
                continue
            candidates.extend(parsed if isinstance(parsed, list) else [parsed])
        if chunks and not candidates:
            raise ActMcpError(f"act-mcp returned a body that is neither JSON nor SSE: {body[:200]}")
        for candidate in candidates:
            if isinstance(candidate, dict) and (wanted is None or candidate.get("id") == wanted):
                if "error" in candidate:
                    error = candidate["error"]
                    raise ActMcpError(f"act-mcp returned a JSON-RPC error: {error}")
                return candidate.get("result") or {}
        if candidates and all(isinstance(c, dict) and "error" in c for c in candidates):
            raise ActMcpError(f"act-mcp returned a JSON-RPC error: {candidates[0]['error']}")
        raise ActMcpError(f"act-mcp returned no response for request id {wanted!r}")

    def initialize(self) -> dict[str, Any]:
        result = self._request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": "1.0"},
                },
            },
            expect=True,
        )
        result = result or {}
        self.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        self.server_info = result.get("serverInfo") or {}
        self.init_result = result
        self._request({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(20):  # a paged catalogue that never ends is a bug we stop reporting
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = self._request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params}, expect=True)
            page = (result or {}).get("tools") or []
            tools.extend(page)
            cursor = (result or {}).get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return (
            self._request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                expect=True,
            )
            or {}
        )


def timeout_seconds(cli_value: float | None, configured: float | None = None) -> float:
    """Seconds per request: --timeout, else $ACT_MCP_TIMEOUT, else the mcp config, else 900."""
    for value in (cli_value, _env_timeout(), configured):
        if value:
            return float(value)
    return 900.0


def _env_timeout() -> float | None:
    raw = os.environ.get("ACT_MCP_TIMEOUT")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


# --- tool and argument selection ----------------------------------------------
def _normal(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def choose_tool(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """The tool that runs a workflow, chosen by name; ``ValueError`` when there is none."""
    usable = [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]
    for pattern in TOOL_PREFERENCE:
        for tool in usable:
            if re.search(pattern, str(tool["name"]).lower()):
                return tool
    named = ", ".join(sorted(str(tool.get("name")) for tool in usable)) or "(none)"
    raise ValueError(
        "the act-mcp server exposes no workflow-running tool (tools: %s)\n"
        "  fix: pass --tool NAME (see: %s _act_mcp.py discover)" % (named, Path(__file__).name)
    )


def schema_properties(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    return properties if isinstance(properties, dict) else {}


def pick_argument(tool: dict[str, Any], candidates: tuple[str, ...], label: str, *, optional: bool) -> str | None:
    """Which inputSchema key carries this value, read off the tool's own schema."""
    properties = schema_properties(tool)
    if not properties:
        # No schema to read: only the workspace is worth passing blind, and only under
        # the conventional name; anything else would be a guess.
        if optional:
            return None
        return candidates[0]
    normalised = {_normal(name): name for name in properties}
    for candidate in candidates:
        key = normalised.get(_normal(candidate))
        if key:
            return key
    if optional:
        return None
    raise ValueError(
        f"the act-mcp tool {tool.get('name')!r} takes no {label} argument "
        f"(inputSchema properties: {', '.join(sorted(properties)) or 'none'})\n"
        f"  fix: pass --workspace-arg NAME with the key that means the repository root, "
        "or run the gates directly with --via direct"
    )


def _has(arguments: dict[str, Any], name: str) -> bool:
    """Whether ``arguments`` already carries ``name`` under any spelling of it."""
    return _normal(name) in {_normal(key) for key in arguments}


def derived_values(
    tool: dict[str, Any],
    arguments: dict[str, Any],
    *,
    client: McpHttp,
    workspace: Path,
    category: str | None,
    commit: str | None,
) -> None:
    """Fill the arguments a workspace-bound server insists on but this client can work out.

    Every value is one the caller has already established in another form: the commit is
    this checkout's HEAD (the server resolves commits locally and never fetches, so this
    is what pins the run to the code being gated), the category is the server's own hint
    from ``list_tasks``, and the wait/timeout/log-tail trio is what turns one call into a
    verdict with a readable log behind it. A required argument this client has no value
    for stops the run here rather than arriving as a server-side validation error.
    """
    schema = tool.get("inputSchema") or {}
    declared_required = [str(name) for name in (schema.get("required") or [])]
    required = {_normal(name) for name in declared_required}

    def declare(candidates: tuple[str, ...], label: str, value: Any, *, only_if_required: bool) -> None:
        """Send one derived argument, if the server declares it and it is not already set."""
        key = pick_argument(tool, candidates, label, optional=True)
        if not key or _has(arguments, key) or (only_if_required and _normal(key) not in required):
            return
        if value is None:
            raise ActMcpError(
                f"the act-mcp tool {tool.get('name')!r} requires {key!r} ({label}) and this client has "
                f"no value for it\n  fix: pass --arg {key}=VALUE for the tree at {workspace}, or run "
                "the gates directly with --via direct"
            )
        arguments[key] = value

    declare(COMMIT_ARGS, "the commit to run", commit or head_commit(workspace), only_if_required=False)
    declare(CATEGORY_ARGS, "what kind of job this is", category, only_if_required=False)
    # Two knobs about the answer this client consumes: `wait` is what makes one call return
    # a verdict instead of a run id this client would then have to poll, and the log tail
    # has to be long enough to still hold the coverage line after the suite scrolled past
    # it. The run's own budget is only sent when insisted on, and is set under this
    # client's socket timeout so the server reports a timeout rather than this call dying
    # first and losing sight of a run that goes on without it.
    declare(WAIT_ARGS, "the terminal answer, not a run id", True, only_if_required=False)
    declare(LOG_TAIL_ARGS, "how much job log comes back", LOG_TAIL_BYTES, only_if_required=False)
    declare(
        RUN_TIMEOUT_ARGS,
        "the run's own wall-clock budget",
        max(30, int(client.timeout) - 60),
        only_if_required=True,
    )
    missing = [
        name
        for name in sorted(declared_required)
        if not _has(arguments, name) and pick_argument(tool, (name,), name, optional=True) is not None
    ]
    if missing:
        raise ActMcpError(
            f"the act-mcp tool {tool.get('name')!r} requires "
            f"{', '.join(repr(name) for name in missing)}, which this client has no value for\n"
            "  fix: pass --arg NAME=VALUE for each one (see --help), or run the gates directly "
            "with --via direct"
        )


def build_arguments(
    tool: dict[str, Any],
    *,
    workspace: Path,
    job: str | None,
    workflow: str | None,
    mode: WorkspaceMode,
    extra: list[str],
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if mode.argument:
        arguments[mode.argument] = str(workspace)
    if job:
        job_key = pick_argument(tool, JOB_ARGS, "job", optional=True)
        if job_key:
            arguments[job_key] = job
    if workflow:
        workflow_key = pick_argument(tool, WORKFLOW_ARGS, "workflow file", optional=True)
        if workflow_key and workflow_key != job_key:
            arguments[workflow_key] = workflow
    for spec in extra:
        name, sep, value = spec.partition("=")
        if not sep:
            raise ValueError(f"--arg expects NAME=VALUE, got {spec!r}")
        arguments[name] = value
    return arguments


def _json_from_text(text: str) -> dict[str, Any]:
    """The JSON object a tool's text answer is, or contains - if it is or does.

    act-mcp answers a finished run as JSON in a text block and, depending on version,
    also as ``structuredContent``; reading both means the verdict is found whichever way
    the server chose to say it.
    """
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1] if "{" in text and "}" in text else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def result_text(result: dict[str, Any]) -> str:
    """Every text block a tool result carries, joined - the job log we can read."""
    chunks: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") in (None, "text"):
            chunks.append(str(block.get("text", "")))
    structured = result.get("structuredContent")
    if structured is not None:
        chunks.append(json.dumps(structured, indent=1, sort_keys=True))
    return "\n".join(chunks)


def run_status(result: dict[str, Any], text: str) -> str | None:
    """The run's terminal status as the server named it, if it named one."""
    for source in (result.get("structuredContent"), _json_from_text(text)):
        if not isinstance(source, dict):
            continue
        for holder in (source, source.get("run"), source.get("run_view")):
            if isinstance(holder, dict):
                for key in ("status", "state", "run_status"):
                    value = holder.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip().lower()
    match = re.search(r"\"(?:status|state)\"\s*:\s*\"([A-Za-z_]+)\"", text)
    return match.group(1).lower() if match else None


def server_busy(result: dict[str, Any], text: str) -> bool:
    """Whether the server refused for a reason that says nothing about this job.

    act-mcp runs one job per workspace with no queue, so a concurrent run answers
    ``workspace_busy``. That is an unusable runner right now, not a red gate, and the
    caller must be able to tell the two apart.
    """
    return "workspace_busy" in text or "workspace_busy" in json.dumps(result.get("structuredContent") or {})


def refusal_reason(result: dict[str, Any], text: str) -> str | None:
    """Why the server declined to run the job at all, in its own words - or ``None``.

    A structured ``error`` answer (bad argument, unknown task, unknown commit, busy
    workspace) is a statement about the *runner*, not about the workflow: the job never
    started, so it has no verdict and the caller must fall back to the gate scripts rather
    than report a build that never ran. A workflow that ran and failed answers with a
    ``status`` instead - that one is a real, red verdict.
    """
    if server_busy(result, text):
        return "workspace_busy: another run already holds that workspace (act-mcp has no queue)"
    for source in (result.get("structuredContent"), _json_from_text(text)):
        if isinstance(source, dict) and isinstance(source.get("error"), dict):
            error = source["error"]
            code = str(error.get("code") or "error")
            message = str(error.get("message") or "").strip()
            return f"{code}: {message}" if message else code
    match = re.search(r"^\S+:\s*(\w+):\s*argument", text, re.MULTILINE)
    return f"invalid arguments ({match.group(1)})" if match else None


def job_failed(result: dict[str, Any], text: str) -> bool:
    """Whether the tool itself said the job was red.

    ``isError`` is the protocol's own word for it, but it is not enough on its own: a
    workflow that ran and failed is a *successful* tool call whose payload says
    ``status: failed``, and reading only ``isError`` would report that build green. The
    markers below are what this repository's gates print, so even a server that flattens
    the whole run into a text block fails the run instead of shrugging.
    """
    if result.get("isError"):
        return True
    if run_status(result, text) in FAILED_STATUSES:
        return True
    if re.search(r"coverage floor failed", text, re.MULTILINE):
        return True
    if re.search(r"^REGRESSION\s+", text, re.MULTILINE):
        return True
    if re.search(r"^\s*\*\*FAIL\*\*", text, re.MULTILINE):
        return True
    return False


# --- subcommands ---------------------------------------------------------------
def connect(args: argparse.Namespace) -> tuple[McpHttp, dict[str, Any]]:
    """(client, tool) after config, handshake, catalogue and selection - or ActMcpError."""
    url = args.url or os.environ.get("ACT_MCP_URL") or None
    server = ""
    configured_timeout: float | None = None
    if not url:
        servers = read_mcp_config(Path(args.config))
        server, url, configured_timeout = pick_server(servers, args.server)
    client = McpHttp(url, timeout=timeout_seconds(args.timeout, configured_timeout))
    client.initialize()
    tools = client.list_tools()
    client.tools = tools
    if args.tool:
        tool = next((t for t in tools if isinstance(t, dict) and t.get("name") == args.tool), None)
        if tool is None:
            names = ", ".join(sorted(str(t.get("name")) for t in tools if isinstance(t, dict)))
            raise ActMcpError(
                f"the act-mcp server exposes no tool named {args.tool!r} (tools: {names or '(none)'})\n"
                "  fix: pick one of those names, or run the gates directly with --via direct"
            )
    else:
        try:
            tool = choose_tool(tools)
        except ValueError as exc:
            raise ActMcpError(str(exc)) from None
    if server:
        print(f"server:    act-mcp ({server}) at {url}", flush=True)
    else:
        print(f"server:    act-mcp at {url}", flush=True)
    print(f"tool:      {tool.get('name')}", flush=True)
    return client, tool


def cmd_discover(args: argparse.Namespace) -> int:
    """What would be used, and whether it can be used here - the runner's go/no-go probe.

    ``scripts/ci/post_commit.sh`` calls this before choosing a runner, so its exit status
    is the answer to "can act-mcp gate this checkout", and its lines have to say which
    tree a run would measure.
    """
    client, tool = connect(args)
    properties = schema_properties(tool)
    print(f"schema:    {', '.join(sorted(properties)) or '(no inputSchema)'}", flush=True)
    workspace = resolve_workspace(args.workspace or ".")
    try:
        mode = plan_workspace(
            tool=tool,
            client=client,
            requested=workspace,
            override=args.workspace_arg,
            server_bound=args.server_bound,
        )
    except ActMcpError as exc:
        print(f"error:     {exc}", file=sys.stderr, flush=True)
        return UNAVAILABLE
    print(f"workspace: {mode.note}", flush=True)
    tasks = list_tasks_tool(client.tools)
    if tasks:
        print(f"tasks:     {tasks} (read-only; supplies the category hint)", flush=True)
    print(f"root:      {workspace}", flush=True)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Print the call that would be made, without making it - what the docs show."""
    workspace = resolve_workspace(args.workspace)
    url = args.url or os.environ.get("ACT_MCP_URL") or None
    if not url:
        # A plan is the call, not the connection. A checkout with no mcp config - or a
        # --config that does not exist - still gets to see what would be sent, with the
        # url left null; `run` is where an unusable server becomes an error.
        try:
            _server, url, _timeout = pick_server(read_mcp_config(Path(args.config)), args.server)
        except ActMcpError:
            url = None
    tool_name = args.tool or "run_workflow"
    arguments = {args.workspace_arg or "workspace": str(workspace)}
    if args.job:
        arguments["job"] = args.job
    if args.workflow:
        arguments["workflow"] = args.workflow
    print(json.dumps({"url": url, "name": tool_name, "arguments": arguments}, indent=2, sort_keys=True))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace)
    print(f"workspace: {workspace} (resolved from --workspace)", flush=True)
    client, tool = connect(args)
    mode = plan_workspace(
        tool=tool,
        client=client,
        requested=workspace,
        override=args.workspace_arg,
        server_bound=args.server_bound,
    )
    print(f"mode:      {mode.note}", flush=True)
    commit = args.commit or head_commit(workspace)
    category = args.category
    if category is None:
        tasks = list_tasks_tool(getattr(client, "tools", []))
        if tasks:
            category = category_hint(client, tasks, workflow=args.workflow, job=args.job)
    if category is None and args.job:
        # No catalogue to ask (or it had no hint): the job's own name is the hint.
        lowered = args.job.lower()
        category = "coverage" if "coverage" in lowered else "benchmark" if "bench" in lowered else None
    arguments = build_arguments(
        tool,
        workspace=workspace,
        job=args.job,
        workflow=args.workflow,
        mode=mode,
        extra=args.arg,
    )
    derived_values(
        tool,
        arguments,
        client=client,
        workspace=workspace,
        category=category,
        commit=commit,
    )
    print(f"call:      tools/call {tool['name']} {json.dumps(arguments, sort_keys=True)}", flush=True)
    result = client.call_tool(str(tool["name"]), arguments)
    text = result_text(result)
    print(text, flush=True)
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        Path(args.log).write_text(text)
    status = run_status(result, text)
    # A refusal is checked before a verdict, and only when the run never named a status:
    # "the job is red" and "the runner would not start the job" are different answers, and
    # only the second one is allowed to send the caller back to the gate scripts.
    refused = refusal_reason(result, text) if status is None else None
    if refused:
        raise ActMcpError(f"the act-mcp server would not run {args.job or tool['name']!r}: {refused}")
    if job_failed(result, text):
        said = f" (status: {status})" if status else ""
        print(f"act-mcp:   {args.job or tool['name']} reported a failing job{said}", file=sys.stderr, flush=True)
        return 1
    print(
        f"act-mcp:   {args.job or tool['name']} reported success"
        + (f" (status: {status})" if status else ""),
        flush=True,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="_act_mcp.py", description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=".pi/mcp.json", help="MCP config to read the server url from")
    parser.add_argument("--server", default=None, help="server name inside mcpServers (default: the act-mcp one)")
    parser.add_argument(
        "--url", default=None, help="server url, overriding --config entirely (default: $ACT_MCP_URL, then --config)"
    )
    parser.add_argument("--tool", default=None, help="tool name, overriding the built-in preference order")
    parser.add_argument("--timeout", type=float, default=None, help="seconds per request (default $ACT_MCP_TIMEOUT)")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="print the server, the tool and its input schema")
    discover.add_argument("--workspace-arg", default=None, help="override the argument that carries the root")
    discover.add_argument(
        "--workspace", default=None, help="the checkout to check the server against (default: this one)"
    )
    discover.add_argument(
        "--server-bound",
        action="store_true",
        help="accept a server that runs its own checkout, whoever owns it",
    )
    discover.set_defaults(func=cmd_discover)

    plan = sub.add_parser("plan", help="print the tool call that would be made, and make nothing")
    plan.add_argument("--workspace", required=True, help="repository root holding .github/workflows")
    plan.add_argument("--workspace-arg", default=None, help="argument name to carry it under")
    plan.add_argument("--job", default=None)
    plan.add_argument("--workflow", default=None)
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="run one job and echo the tool's output")
    run.add_argument("--workspace", required=True, help="repository root holding .github/workflows")
    run.add_argument("--workspace-arg", default=None, help="override the argument that carries the root")
    run.add_argument(
        "--server-bound",
        action="store_true",
        help="send no workspace argument even if the server's own words name another checkout",
    )
    run.add_argument("--job", default=None, help="job id to run, e.g. coverage")
    run.add_argument("--workflow", default=None, help="workflow file to run it from, e.g. ci.yml")
    run.add_argument(
        "--category",
        default=None,
        help="what kind of job this is (act-mcp: coverage, benchmark); default: list_tasks' hint",
    )
    run.add_argument("--commit", default=None, help="commit the server should resolve; default: this HEAD")
    run.add_argument(
        "--arg", action="append", default=[], metavar="NAME=VALUE", help="extra tool argument; repeatable"
    )
    run.add_argument("--log", default=None, help="also write the tool's output here, for the summary block")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ActMcpError as exc:
        print(f"act-mcp unavailable: {exc}", file=sys.stderr, flush=True)
        return UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
