#!/usr/bin/env bash
# Everything CI runs after a commit: type check, tests + coverage, benchmarks
# (ADR-0001 §12). This is scripts/ci/post_commit.sh with this repository's interpreter
# and is the command an act-mcp style runner is expected to call.
#
# Usage: ./scripts/dev/check.sh            # all three gates
#        ./scripts/dev/check.sh --quick   # skip the benchmark (slowest gate)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
exec ./scripts/ci/post_commit.sh "$@"
