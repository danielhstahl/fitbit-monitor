#!/usr/bin/env bash
# Benchmark gate for fitbit-monitor: the one place the perf gate's command line lives.
#
# This is the gate; `.github/workflows/bench.yml` only calls it, which is what keeps a
# laptop and a runner failing the same way (AGENTS.md: the action holds no logic of its
# own). It runs the committed benchmark suite - scripts/bench/bench_plugin_system.py
# over a synthetic corpus - and writes two reports:
#
#   bench-results.json   case, mean, stddev, iterations, the value that gated, the
#                        baseline behind it, and an environment summary
#   bench-summary.md     the same numbers as a table, for the PR comment and the job
#                        summary
#
# and it ends by naming both, the way the coverage gate does:
#
#   artifact bench-results-json: bench-results.json
#   artifact bench-summary-md: bench-summary.md
#
# It exits nonzero when any tracked case regresses more than the tolerance (default
# 15%) against the committed baseline, benchmarks/baseline.json - which is only ever
# refreshed on purpose:
#
#     ./scripts/dev/bench.sh --update-baseline && git add benchmarks/baseline.json
#
# CI never passes --update-baseline, so no run can re-baseline its own regression away
# (docs/ci.md).
#
# Usage: scripts/ci/bench.sh [--no-install] [options]
#   --no-install                skip the `pip install -e ".[dev]"` step (CI passes this)
#   --json-out PATH             results document to write    (default bench-results.json)
#   --markdown-out PATH         markdown summary to write    (default bench-summary.md)
#   --artifact-name NAME        artifact the JSON becomes    (default bench-results-json)
#   --markdown-artifact-name N  artifact the markdown becomes(default bench-summary-md)
#   --baseline PATH             baseline to compare against  (default benchmarks/baseline.json)
#   --tolerance PCT             allowed regression per case, percent (default 15)
#   --days N                    corpus size (default 90; anything else is not gated,
#                               because a different corpus is a different quantity)
#   --repeats N                 samples per case (default 9; the best one gates)
#   --update-baseline           record these numbers as the baseline; never passed by CI
#   --no-gate                   report only; never fail
#   -h, --help                  this text
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
. scripts/_python.sh

JSON_OUT=bench-results.json
MARKDOWN_OUT=bench-summary.md
ARTIFACT_NAME=bench-results-json
MARKDOWN_ARTIFACT_NAME=bench-summary-md
INSTALL=1
EXTRA=()

usage() {
    sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '/^set -euo pipefail/d; s/^# \{0,1\}//'
}

# Accepts both `--flag value` and `--flag=value`, so the workflow and a hand-typed
# command line look the same from here (same trick as scripts/ci/coverage.sh).
need_value() {
    if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "error: $1 needs a value (see --help)" >&2
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-install)
            INSTALL=0
            shift
            ;;
        --json-out)
            need_value "$@"
            JSON_OUT="${2:-}"
            shift 2
            ;;
        --json-out=*)
            JSON_OUT="${1#*=}"
            shift
            ;;
        --markdown-out)
            need_value "$@"
            MARKDOWN_OUT="${2:-}"
            shift 2
            ;;
        --markdown-out=*)
            MARKDOWN_OUT="${1#*=}"
            shift
            ;;
        --artifact-name)
            need_value "$@"
            ARTIFACT_NAME="${2:-}"
            shift 2
            ;;
        --artifact-name=*)
            ARTIFACT_NAME="${1#*=}"
            shift
            ;;
        --markdown-artifact-name)
            need_value "$@"
            MARKDOWN_ARTIFACT_NAME="${2:-}"
            shift 2
            ;;
        --markdown-artifact-name=*)
            MARKDOWN_ARTIFACT_NAME="${1#*=}"
            shift
            ;;
        --baseline)
            need_value "$@"
            EXTRA+=(--baseline "${2:-}")
            shift 2
            ;;
        --baseline=*)
            EXTRA+=("$1")
            shift
            ;;
        --tolerance)
            need_value "$@"
            EXTRA+=(--tolerance "${2:-}")
            shift 2
            ;;
        --tolerance=*)
            EXTRA+=("$1")
            shift
            ;;
        --days)
            need_value "$@"
            EXTRA+=(--days "${2:-}")
            shift 2
            ;;
        --days=*)
            EXTRA+=("$1")
            shift
            ;;
        --repeats)
            need_value "$@"
            EXTRA+=(--repeats "${2:-}")
            shift 2
            ;;
        --repeats=*)
            EXTRA+=("$1")
            shift
            ;;
        --update-baseline)
            EXTRA+=(--update-baseline)
            shift
            ;;
        --no-gate)
            EXTRA+=(--no-gate)
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument '$1' (see --help)" >&2
            exit 2
            ;;
    esac
done

if [[ "$INSTALL" -eq 1 ]]; then
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -e ".[dev]"
fi

echo "== benchmarks (scripts/bench/bench_plugin_system.py vs benchmarks/baseline.json)"
exec "$PYTHON" scripts/bench/bench_plugin_system.py \
    --json-out "$JSON_OUT" \
    --markdown-out "$MARKDOWN_OUT" \
    --artifact-name "$ARTIFACT_NAME" \
    --markdown-artifact-name "$MARKDOWN_ARTIFACT_NAME" \
    --step-summary \
    "${EXTRA[@]+"${EXTRA[@]}"}"
