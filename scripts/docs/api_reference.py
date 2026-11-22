#!/usr/bin/env python3
"""Generate the API reference section of ``docs/plugins.md`` from the docstrings.

The plugin SDK's reference is generated, not hand-copied, so it cannot drift from
the code it documents:

    python scripts/docs/api_reference.py            # print it
    python scripts/docs/api_reference.py --check     # fail if docs/plugins.md is stale
    python scripts/docs/api_reference.py --write     # rewrite the generated block

The block lives between ``<!-- BEGIN GENERATED API REFERENCE -->`` and
``<!-- END GENERATED API REFERENCE -->`` in the doc. ``tests/test_docs.py`` runs
``--check`` in CI, which is what makes "update the docs" a hard requirement rather
than a courtesy.
"""

from __future__ import annotations

import argparse
import enum
import inspect
import re
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import fitbit_monitor.plugins as sdk  # noqa: E402
from fitbit_monitor.plugins import MonitorPlugin, PluginOption  # noqa: E402

DOC_PATH = REPO_ROOT / "docs" / "plugins.md"
BEGIN = "<!-- BEGIN GENERATED API REFERENCE -->"
END = "<!-- END GENERATED API REFERENCE -->"

#: What gets a heading in the reference, in order: (symbol, one-line role).
CLASSES: list[tuple[str, str]] = [
    ("MonitorPlugin", "subclass this"),
    ("PluginContext", "what run() receives"),
    ("PluginOption", "declare a config option"),
    ("MetricResult", "a value a plugin computed"),
    ("Alert", "something the user should hear about"),
    ("Evidence", "one data point quoted by an alert"),
    ("Severity", "alert severity"),
    ("PluginError", "base class for plugin failures"),
    ("PluginRegistrationError", "object is not a usable plugin"),
    ("PluginImportError", "the module raised on import"),
    ("PluginConfigError", "the plugin rejected its options"),
    ("PluginRuntimeError", "run() raised"),
    ("PluginTimeout", "wall-clock budget exhausted"),
]

FUNCTIONS: list[tuple[str, str]] = [
    ("run_plugins", "run plugins over a window"),
    ("validate_plugin_class", "check a candidate class"),
    ("downsample", "bucket points by hour/day/week"),
    ("daily_values", "{date: value} for one field"),
    ("aggregate", "collapse a list of numbers"),
]

CONSTANTS: list[tuple[str, str]] = [
    ("PLUGIN_API_VERSION", "the contract version this host serves"),
    ("MIN_PLUGIN_API_VERSION", "oldest plugin API still accepted"),
    ("ENTRY_POINT_GROUP", "the entry-point group plugins publish into"),
]


def _clean(text: str) -> str:
    """Make a docstring line readable as markdown: no Sphinx roles, no double backticks."""
    text = re.sub(r":(?:class|attr|meth|obj|data|func):`([^`]+)`", r"\1", text)
    text = text.replace("``", "`")
    return text.strip().rstrip(".")


def _signature(obj: object) -> str:
    try:
        text = str(inspect.signature(obj))  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - builtins
        text = "()"
    text = text.replace("'", "").replace('"', "")
    text = text.replace(" -> None", "").replace(" -> NoneType", "")
    for prefix in ("fitbit_monitor.plugins.models.", "fitbit_monitor.plugins.contract."):
        text = text.replace(prefix, "")
    return text


def _summary(obj: object) -> str:
    """First sentence or clause of the docstring, as a single markdown line."""
    doc = inspect.getdoc(obj) or ""
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return _clean(line)
    return ""


def _members(cls: type) -> Iterable[str]:
    for name, value in vars(cls).items():
        if name.startswith("_"):
            continue
        if callable(value) or isinstance(value, (property, classmethod, staticmethod)):
            doc = inspect.getdoc(value) or ""
            first = next((line.strip() for line in doc.splitlines() if line.strip()), "")
            if not first:
                continue
            first = _clean(first)
            if callable(value):
                yield f"  - `{name}{_signature(value)}` - {first}"
            else:
                yield f"  - `{name}` - {first}"


def render() -> str:
    lines: list[str] = []
    lines.append("Constants: `PLUGIN_API_VERSION = %s`, minimum accepted `%s`, entry-point group"
                 " `%s`." % (sdk.PLUGIN_API_VERSION, sdk.MIN_PLUGIN_API_VERSION, sdk.ENTRY_POINT_GROUP))
    lines.append("")

    for name, role in CLASSES:
        obj = getattr(sdk, name)
        heading = f"### `{name}` - {role}"
        lines.append(heading)
        if isinstance(obj, type):
            base = ""
            if issubclass(obj, BaseException):
                base = " *(error)*"
                lines.append(f"`{name}{_signature(obj)}{base}`")
            elif obj is MonitorPlugin:
                lines.append(f"`class {name}{_signature(obj)}`")
            else:
                lines.append(f"`class {name}{_signature(obj)}`")
        summary = _summary(obj)
        if summary:
            lines.append("")
            lines.append(summary)
        if isinstance(obj, type) and issubclass(obj, enum.Enum):
            lines.append("")
            lines.append("Values: " + ", ".join(f"`{m.value!r}`" for m in obj) + ".")
        if isinstance(obj, type) and not issubclass(obj, BaseException):
            members = list(_members(obj))
            if members:
                lines.append("")
                lines.append("Members:")
                lines.extend(members)
        if obj is PluginOption:
            lines.append("")
            lines.append(f"Declared types: `{'`, `'.join(PluginOption._TYPES)}`.")  # noqa: SLF001
        if obj is MonitorPlugin:
            lines.append("")
            lines.append(
                "Class attributes a plugin sets: `name`, `summary`, `version`, `datasets`, "
                "`requires_api`, `options`."
            )
        lines.append("")

    for name, role in FUNCTIONS:
        obj = getattr(sdk, name)
        lines.append(f"### `{name}()` - {role}")
        lines.append("")
        lines.append(f"`{name}{_signature(obj)}`")
        summary = _summary(obj)
        if summary:
            lines.append("")
            lines.append(summary)
        lines.append("")

    for name, role in CONSTANTS:
        value = getattr(sdk, name)
        lines.append(f"### `{name}` - {role}")
        lines.append("")
        lines.append(f"`{value!r}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def splice(document: str, block: str) -> str:
    if BEGIN not in document or END not in document:
        raise SystemExit(f"docs error: {DOC_PATH} is missing the generated-block markers")
    head, _, rest = document.partition(BEGIN)
    _, _, tail = rest.partition(END)
    return f"{head}{BEGIN}\n\n{block}\n{END}{tail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the plugin SDK API reference.")
    parser.add_argument("--check", action="store_true", help="fail if the doc block is not up to date")
    parser.add_argument("--write", action="store_true", help="rewrite the doc block in place")
    parser.add_argument("--doc", default=str(DOC_PATH), help="document to read or rewrite")
    args = parser.parse_args(argv)

    block = render()
    doc = Path(args.doc)
    if not args.check and not args.write:
        print(block)
        return 0
    if not doc.exists():
        print(f"error: {doc} does not exist", file=sys.stderr)
        return 2
    text = doc.read_text()
    current = splice(text, block)
    if args.write:
        doc.write_text(current)
        print(f"updated {doc}")
        return 0
    if current != text:
        print(
            f"{doc}: the generated API reference is stale.\n"
            "  fix: python scripts/docs/api_reference.py --write",
            file=sys.stderr,
        )
        return 1
    print(f"{doc}: API reference is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
