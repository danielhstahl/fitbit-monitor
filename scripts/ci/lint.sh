#!/usr/bin/env bash
# Static checks: type checking and byte-compilation of everything we ship.
#
# mypy is the contract's first line of defence - the plugin models and the context
# are typed on purpose, so a plugin author's IDE sees what CI sees.
#
# Usage: scripts/ci/lint.sh [--no-install]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
PYTHON="${PYTHON:-python3}"

if [[ "${1:-}" != "--no-install" ]]; then
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -e ".[dev]"
fi

echo "== mypy"
"$PYTHON" -m mypy

echo
echo "== pyflakes (unused imports, undefined names, f-strings without placeholders)"
"$PYTHON" -m pyflakes src scripts tests

echo
echo "== byte-compile package, scripts and tests"
"$PYTHON" -m compileall -q src scripts tests

echo
echo "== shell scripts parse"
for script in scripts/ci/*.sh; do
    bash -n "$script"
    echo "ok  $script"
done
