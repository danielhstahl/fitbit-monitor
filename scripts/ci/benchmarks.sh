#!/usr/bin/env bash
# Benchmark gate for fitbit-monitor.
#
# Measures the plugin system and fails if any case regressed more than the
# tolerance in benchmarks/baseline.json. Re-record the baseline on purpose:
#
#     python scripts/bench/bench_plugin_system.py --update-baseline
#     git add benchmarks/baseline.json
#
# Usage: scripts/ci/benchmarks.sh [--no-install] [extra args passed through]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
PYTHON="${PYTHON:-python3}"
ARGS=()

for arg in "$@"; do
    if [[ "$arg" == "--no-install" ]]; then
        continue
    fi
    ARGS+=("$arg")
done

if [[ "${1:-}" != "--no-install" ]]; then
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -e ".[dev]"
fi

if [[ ! -f benchmarks/baseline.json ]]; then
    echo "error: benchmarks/baseline.json is missing - run the benchmark with --update-baseline" >&2
    exit 2
fi

exec "$PYTHON" scripts/bench/bench_plugin_system.py "${ARGS[@]+"${ARGS[@]}"}"
