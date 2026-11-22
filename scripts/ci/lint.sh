#!/usr/bin/env bash
# Static checks: type checking, linting, and byte-compilation of everything we ship.
#
# mypy is the contract's first line of defence - the plugin models and the context
# are typed on purpose, so a plugin author's IDE sees what CI sees. ruff is the linter
# (ADR-0001 §10): its F rules are pyflakes' rules, so this gate is pyflakes plus more.
#
# Usage: scripts/ci/lint.sh [--no-install]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
. scripts/_python.sh

if [[ "${1:-}" != "--no-install" ]]; then
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -e ".[dev]"
fi

echo "== mypy"
"$PYTHON" -m mypy

echo
if ! "$PYTHON" -c "import ruff" 2>/dev/null; then
    echo "error: ruff is not installed in $PYTHON" >&2
    echo "  fix: ./scripts/dev/bootstrap.sh   (or: uv sync --extra dev)" >&2
    exit 2
fi
echo "== ruff check (lint tier 1 - see [tool.ruff.lint] in pyproject.toml)"
"$PYTHON" -m ruff check src scripts tests

echo
echo "== byte-compile package, scripts and tests"
"$PYTHON" -m compileall -q src scripts tests

echo
echo "== GitHub workflow YAML parses (act-mcp runs these same workflows locally)"
"$PYTHON" - <<'PYEOF'
import glob
import sys
import yaml

paths = sorted(glob.glob(".github/workflows/*.yml"))
if not paths:
    print("FAIL  no .github/workflows/*.yml found", file=sys.stderr)
    raise SystemExit(1)
for path in paths:
    with open(path) as handle:
        doc = yaml.safe_load(handle)
    jobs = (doc or {}).get("jobs") or {}
    if not jobs:
        print(f"FAIL  {path} declares no jobs", file=sys.stderr)
        raise SystemExit(1)
    print(f"ok  {path} ({', '.join(jobs)})")
PYEOF

echo
echo "== shell scripts parse"
for script in scripts/*.sh scripts/*/*.sh; do
    bash -n "$script"
    echo "ok  $script"
done
