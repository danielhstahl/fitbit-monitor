#!/usr/bin/env bash
# Code coverage gate for fitbit-monitor.
#
# The workflow file only calls this script, so a developer and CI run the same
# command and get the same failure. Coverage policy lives in pyproject.toml
# ([tool.coverage.*]); change the floor there, not here.
#
# Usage: scripts/ci/coverage.sh [--no-install]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
PYTHON="${PYTHON:-python3}"

if [[ "${1:-}" != "--no-install" ]]; then
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -e ".[dev]"
fi

echo "== test suite (unittest, so pytest is optional)"
"$PYTHON" -m unittest discover -s tests -t tests -q

echo
echo "== coverage"
rm -f .coverage
"$PYTHON" -m coverage run --branch --source=src/fitbit_monitor -m pytest -q tests
"$PYTHON" -m coverage report
"$PYTHON" -m coverage xml -o coverage.xml
echo
echo "coverage report written to coverage.xml"
