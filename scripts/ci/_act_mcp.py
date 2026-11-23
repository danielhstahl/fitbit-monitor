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
    2  act-mcp could not be used (no config, no route, no usable tool, no workspace
       argument in its schema) - the caller falls back to running the gate directly

Two honesty rules. The workspace is never guessed at: ``--workspace`` is required, is
made absolute, and is checked for ``.github/workflows`` before anything is sent. And
the argument *names* the server wants are read out of the tool's own ``inputSchema``
rather than hardcoded, because the server is a separate binary whose parameter names
this repository does not control - when no workspace-shaped argument exists, this
prints the tool's schema and exits ``2`` instead of silently dropping the one argument
that was the whole point.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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
JOB_ARGS = ("job", "jobid", "jobname", "job_id", "job_name")
WORKFLOW_ARGS = ("workflow", "workflowfile", "workflow_file", "workflowpath", "file", "name")

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
        self._request({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(20):  # a paged catalogue that never ends is a bug we stop reporting
            params = {"cursor": cursor} if cursor else {}
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
        if candidate in normalised:
            return normalised[candidate]
    if optional:
        return None
    raise ValueError(
        f"the act-mcp tool {tool.get('name')!r} takes no {label} argument "
        f"(inputSchema properties: {', '.join(sorted(properties)) or 'none'})\n"
        f"  fix: pass --workspace-arg NAME with the key that means the repository root, "
        "or run the gates directly with --via direct"
    )


def build_arguments(
    tool: dict[str, Any],
    *,
    workspace: Path,
    job: str | None,
    workflow: str | None,
    workspace_arg: str | None,
    extra: list[str],
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    key = workspace_arg or pick_argument(tool, WORKSPACE_ARGS, "workspace", optional=False)
    arguments[key] = str(workspace)
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


def job_failed(result: dict[str, Any], text: str) -> bool:
    """Whether the tool itself said the job was red.

    ``isError`` is the protocol's own word for it; the markers below are what this
    repository's gates print, so a server that reports a failed job as a plain text
    result still fails the run instead of shrugging.
    """
    if result.get("isError"):
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
    client, tool = connect(args)
    properties = schema_properties(tool)
    print(f"schema:    {', '.join(sorted(properties)) or '(no inputSchema)'}", flush=True)
    try:
        key = args.workspace_arg or pick_argument(tool, WORKSPACE_ARGS, "workspace", optional=False)
        print(f"workspace: --{key}", flush=True)
    except ValueError as exc:
        print(f"error:     {exc}", file=sys.stderr, flush=True)
        return UNAVAILABLE
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
    arguments = build_arguments(
        tool,
        workspace=workspace,
        job=args.job,
        workflow=args.workflow,
        workspace_arg=args.workspace_arg,
        extra=args.arg,
    )
    print(f"call:      tools/call {tool['name']} {json.dumps(arguments, sort_keys=True)}", flush=True)
    result = client.call_tool(str(tool["name"]), arguments)
    text = result_text(result)
    print(text, flush=True)
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        Path(args.log).write_text(text)
    if job_failed(result, text):
        print(f"act-mcp:   {args.job or tool['name']} reported a failing job", file=sys.stderr, flush=True)
        return 1
    print(f"act-mcp:   {args.job or tool['name']} reported success", flush=True)
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
    run.add_argument("--job", default=None, help="job id to run, e.g. coverage")
    run.add_argument("--workflow", default=None, help="workflow file to run it from, e.g. ci.yml")
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
