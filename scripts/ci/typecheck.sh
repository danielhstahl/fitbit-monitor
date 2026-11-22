#!/usr/bin/env bash
# Type-check gate for fitbit-monitor: mypy over the package, and nothing else.
#
# Split out of scripts/ci/lint.sh so that "just check the types" has a name of its
# own and one command line (ADR-0001 §10, §12). lint.sh calls this file, so the two
# can never disagree about what a type check means. mypy's settings live in
# pyproject.toml [tool.mypy]; change the policy there, not here.
#
# Usage: scripts/ci/typecheck.sh [--no-install]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
. scripts/_python.sh

if [[ "${1:-}" != "--no-install" ]]; then
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -e ".[dev]"
fi

echo "== mypy"
exec "$PYTHON" -m mypy
