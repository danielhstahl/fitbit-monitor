#!/usr/bin/env bash
# Benchmark gate: the plugin system against benchmarks/baseline.json (ADR-0001 §9).
#
# Usage: ./scripts/dev/bench.sh                       # compare against the baseline
#        ./scripts/dev/bench.sh --days 30            # faster, less representative
#        ./scripts/dev/bench.sh --update-baseline    # re-record (then git add it)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
exec ./scripts/ci/benchmarks.sh --no-install "$@"
