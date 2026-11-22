#!/usr/bin/env bash
# Tests plus the coverage floor (ADR-0001 §7, §8). The coverage floor itself lives in
# pyproject ([tool.coverage.report] fail_under); this only runs the gate.
#
# To run a subset, use the runner directly:
#   .venv/bin/python -m pytest -q tests/test_store_reader.py
#
# Usage: ./scripts/dev/test.sh
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
exec ./scripts/ci/coverage.sh --no-install
