#!/usr/bin/env bash
# Type check and lint, the way CI runs them (ADR-0001 §10).
#
# Thin by design: scripts/ci/lint.sh is the gate and this only points it at this
# repository's interpreter. Two names for the same thing is the point - a contributor
# and a runner cannot drift apart if one of them is a symlink-shaped wrapper.
#
# Usage: ./scripts/dev/lint.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
. scripts/_python.sh
exec ./scripts/ci/lint.sh --no-install
