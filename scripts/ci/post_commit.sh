#!/usr/bin/env bash
# Post-commit gate runner.
#
# The same two jobs the GitHub workflows run - coverage and benchmarks - plus the
# type check, executed here after every commit so a regression is caught before a
# push. This is the script an act-mcp style runner invokes (it needs the repository
# root as its workspace, because that is where .github/workflows/*.yml lives):
#
#     act-mcp --workspace .            # finds .github/workflows/{ci,benchmark}.yml
#     ./scripts/ci/post_commit.sh      # or run it by hand, or as a git hook
#
# Install it as a hook:
#     ln -sf ../../scripts/ci/post_commit.sh .git/hooks/post-commit
#
# Usage: scripts/ci/post_commit.sh [--quick]     (--quick skips the benchmark)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
PYTHON="${PYTHON:-python3}"
QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

if [[ ! -d .github/workflows ]]; then
    echo "error: $(pwd) has no .github/workflows directory" >&2
    echo "  fix: run the runner with --workspace pointed at the repository root that holds" >&2
    echo "       .github/workflows/*.yml (this is the repository root; check your cwd)" >&2
    exit 2
fi

echo "post-commit gates for $(git rev-parse --short HEAD 2>/dev/null || echo 'not-a-git-repo') on $(pwd)"
"$PYTHON" -V

status=0
run() {
    local name="$1"
    shift
    log "$name"
    if "$@"; then
        echo "PASS  $name"
    else
        echo "FAIL  $name"
        status=1
    fi
}

run "type check" ./scripts/ci/lint.sh --no-install
run "tests + coverage" ./scripts/ci/coverage.sh --no-install
if [[ "$QUICK" -eq 1 ]]; then
    log "benchmarks"
    echo "SKIP  benchmarks (--quick)"
else
    run "benchmarks" ./scripts/ci/benchmarks.sh --no-install
fi

log "result"
if [[ "$status" -eq 0 ]]; then
    echo "all post-commit gates passed"
else
    echo "one or more post-commit gates failed - fix before pushing" >&2
fi
exit "$status"
