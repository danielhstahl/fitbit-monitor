#!/usr/bin/env bash
# The coverage gate on its own: suite + coverage floor (ADR-0001 §7, §8).
#
# `test.sh` is the entrypoint the README quickstart names and it runs exactly this
# gate; this file is the same exec under the name CI itself uses, so "run the tests"
# and "run the coverage gate" are both answerable without reading a script to find out
# which one wraps the other. The floors live in pyproject - `[tool.coverage.report]
# fail_under` overall and `[tool.fitbit_monitor.coverage]` per package - and this
# script only runs the gate: coverage policy never lives here. The last lines it prints
# are `coverage: NN.N%` plus one line per package, from scripts/ci/_coverage_report.py.
#
# Usage: ./scripts/dev/coverage.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
. scripts/_python.sh
exec ./scripts/ci/coverage.sh --no-install
