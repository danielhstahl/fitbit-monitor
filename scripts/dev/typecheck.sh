#!/usr/bin/env bash
# Type check only, the way CI runs it (ADR-0001 §10, §12).
#
# `lint.sh` is the whole static gate (types + lint + byte-compile + workflow/shell
# parse); this is the one to reach for when you changed an annotation and want the
# fastest possible answer. Both call the same mypy - scripts/ci/typecheck.sh - which
# is the only thing that makes them provably the same check.
#
# Usage: ./scripts/dev/typecheck.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
. scripts/_python.sh
exec ./scripts/ci/typecheck.sh --no-install
