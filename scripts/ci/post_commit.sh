#!/usr/bin/env bash
# Post-commit gate runner: the coverage job and the benchmark job, after every commit.
#
# This is what runs when a commit lands, and what an act-mcp style runner is meant to
# call (AGENTS.md: if an MCP action runner exists, the coverage and benchmark jobs run
# after every commit). Both jobs are run by name, from the workflow files that define
# them - `ci.yml`'s `coverage` job and `bench.yml`'s `benchmark` job - with the type
# check (`ci.yml`'s `lint` job) along because it is the fastest way to be told you
# broke a type. The last thing printed is one combined PASS/FAIL block naming the
# workspace it resolved, the runner it used, the coverage percentage and the benchmark
# regression verdict; scripts/ci/_gate_summary.py writes that block and its exit status
# is this script's.
#
# The workspace problem this script exists to fix: act-mcp failed in this workspace with
#
#     workspace /Users/.../fitbit-monitor has no `.github` directory ...
#       fix: point --workspace at the repository root that contains .github/workflows/*.yml
#
# because it was handed the caller's working directory instead of the repository root.
# So the root is resolved here, in this order: `--workspace`, else this script's own
# location with symlinks followed (which is what makes it work from `.git/hooks/`),
# else the working directory and its parents, else `git rev-parse --show-toplevel`.
# It is printed with how it was found, and passed to the runner as an absolute path.
# If no candidate holds `.github/workflows` the script stops with a `fix:` line naming
# every path it tried, instead of blaming the workflows for a wrong path.
#
# Runner selection (`--via auto`, the default):
#
#   1. act-mcp - the MCP server configured in .pi/mcp.json, driven by
#      scripts/ci/_act_mcp.py, which reads the argument names out of the tool's own
#      inputSchema. A server that binds its workspace at launch - act-mcp 0.0.4, the one
#      actually configured here - has no such argument, so the client asks the server
#      which tree it owns and only sends a job when that tree is this checkout;
#   2. act     - the `act` binary on PATH ($ACT_BIN), run as `act -W "$ROOT" -j <job>
#      --bind --artifact-server-path <dir>`;
#   3. direct  - scripts/ci/coverage.sh and scripts/ci/bench.sh, the exact command
#      lines the two jobs consist of, run in this checkout.
#
# 1 needs a reachable server and 2 a container runtime; when neither is available the
# gates still run, the summary says `runner: direct`, and nothing pretends a container
# ran. A job the chosen runner cannot run at all falls back to the gate script (turn it
# off with --no-fallback) so the answer is always measured.
#
# Usage: scripts/ci/post_commit.sh [options]
#   --via MODE          auto (default) | act-mcp | act | direct
#   --workspace PATH    repository root to run against (default: resolved, as above)
#   --quick             skip the benchmark (the slowest gate); coverage still runs
#   --no-lint           skip the type check + lint job
#   --no-install        do not create .venv when the interpreter lacks the dev extra
#   --artifacts DIR     act artifact-server path (default: a temp directory)
#   --log-dir DIR       where job logs go (default: a temp directory, kept on failure)
#   --no-fallback       with --via act-mcp|act: fail instead of running the gates here
#   --dry-run           print the workspace, the runner and the calls; run nothing
#   -h, --help          this text
#
# Install it as a git hook (opt-in; see scripts/git-hooks/ and docs/ci.md):
#   ./scripts/git-hooks/install.sh
set -euo pipefail

usage() {
    sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '/^set -euo pipefail/d; s/^# \{0,1\}//'
}

die() {
    echo "error: $1" >&2
    shift
    for line in "$@"; do
        echo "  $line" >&2
    done
    exit 2
}

VIA=auto
WORKSPACE=""
QUICK=0
WITH_LINT=1
BOOTSTRAP=1
FALLBACK=1
DRY_RUN=0
ARTIFACTS=""
LOG_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --via)
            if [[ $# -lt 2 ]]; then
                die "--via needs a value (auto, act-mcp, act or direct)"
            fi
            VIA="$2"
            shift 2
            ;;
        --via=*)
            VIA="${1#*=}"
            shift
            ;;
        --workspace | --repo-root)
            if [[ $# -lt 2 ]]; then
                die "$1 needs a value"
            fi
            WORKSPACE="$2"
            shift 2
            ;;
        --workspace=*)
            WORKSPACE="${1#*=}"
            shift
            ;;
        --quick)
            QUICK=1
            shift
            ;;
        --no-lint)
            WITH_LINT=0
            shift
            ;;
        --no-install)
            BOOTSTRAP=0
            shift
            ;;
        --artifacts)
            if [[ $# -lt 2 ]]; then
                die "--artifacts needs a value"
            fi
            ARTIFACTS="$2"
            shift 2
            ;;
        --artifacts=*)
            ARTIFACTS="${1#*=}"
            shift
            ;;
        --log-dir)
            if [[ $# -lt 2 ]]; then
                die "--log-dir needs a value"
            fi
            LOG_DIR="$2"
            shift 2
            ;;
        --log-dir=*)
            LOG_DIR="${1#*=}"
            shift
            ;;
        --no-fallback)
            FALLBACK=0
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument '$1' (see --help)"
            ;;
    esac
done

case "$VIA" in
    auto | act-mcp | act | direct) ;;
    *) die "--via must be auto, act-mcp, act or direct (got '$VIA')" ;;
esac

# --- workspace resolution ------------------------------------------------------
# This script's own absolute path with symlinks followed, without `readlink -f` (which
# macOS only grew recently). This is what makes it usable as a git hook: the hook file
# is a symlink into scripts/ci, and the repository root is two levels up from the file
# that is really running.
script_path="${BASH_SOURCE[0]}"
while [[ -L "$script_path" ]]; do
    link="$(readlink "$script_path")"
    case "$link" in
        /*) script_path="$link" ;;
        *) script_path="$(cd "$(dirname "$script_path")/$(dirname "$link")" && pwd)/$(basename "$link")" ;;
    esac
done
script_dir="$(cd "$(dirname "$script_path")" && pwd)"

absolute() {
    case "$1" in
        /*) printf '%s\n' "${1%/}" ;;
        *)
            if [[ -d "$1" ]]; then
                printf '%s\n' "$(cd "$1" && pwd)"
            else
                printf '%s\n' "${PWD%/}/$1"
            fi
            ;;
    esac
}

# An explicit --workspace is an instruction, not a hint: if it is not a repository root,
# falling through to some other root would run the gates against a tree nobody asked for.
if [[ -n "$WORKSPACE" ]]; then
    requested="$(absolute "$WORKSPACE")"
    if [[ ! -d "$requested/.github/workflows" ]]; then
        die "--workspace $requested has no .github/workflows directory" \
            "fix: point --workspace at the repository root that contains $requested/.github/workflows/*.yml"
    fi
fi

# Each candidate is "path|how it was found", in the order worth trying: an explicit
# --workspace, then the tree the caller is standing in, then the tree this file happens
# to live in. That order is deliberate. The caller's tree wins because this script is
# routinely run against a checkout that is not the one it came from - a test runs the
# real script against a stub repository of its own - and where the file sits says
# something about the invocation, not about the repository under test. The script's own
# tree is tried last so a git hook (whose cwd is already its own repository) still
# resolves correctly from a bare path with no other hint.
candidates=()
if [[ -n "$WORKSPACE" ]]; then
    candidates+=("$(absolute "$WORKSPACE")|--workspace")
fi
walk="$PWD"
while [[ -n "$walk" && "$walk" != "/" ]]; do
    candidates+=("$walk|the working directory")
    walk="$(dirname "$walk")"
done
if top="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    candidates+=("$top|git rev-parse --show-toplevel")
fi
own_root="$(cd "$script_dir/../.." 2>/dev/null && pwd || true)"
candidates+=("$own_root|$script_path")

ROOT=""
ROOT_SOURCE=""
tried=()
seen=""
for entry in "${candidates[@]}"; do
    path="${entry%|*}"
    how="${entry#*|}"
    if [[ -z "$path" ]]; then
        continue
    fi
    case "|$seen|" in
        *"|$path|"*) continue ;;
    esac
    seen="$seen|$path"
    tried+=("$path")
    if [[ -d "$path/.github/workflows" ]]; then
        ROOT="$path"
        ROOT_SOURCE="$how"
        break
    fi
done

if [[ -z "$ROOT" ]]; then
    joined="$(printf '%s, ' "${tried[@]}")"
    die "no repository root found: none of the paths tried has a .github/workflows directory" \
        "fix: point --workspace at the repository root that contains .github/workflows/*.yml" \
        "tried: ${joined%, }"
fi

# --- recursion guard -----------------------------------------------------------
# scripts/ci/coverage.sh runs this repository's test suite, and that suite runs this
# script (tests/test_ci_post_commit.py drives the runner end to end). A run that
# resolves back to the repository whose suite is live would call coverage.sh, which
# runs the suite again, which calls this script again - a fork bomb with no floor, not a
# gate. coverage.sh exports FITBIT_CI_SUITE as the root whose suite is running, and only
# that exact root is refused here: a test pointed at a stub repository is untouched, a
# --dry-run runs nothing so it cannot recurse, and every ordinary run is unaffected
# because nothing outside a live suite sets the marker.
if [[ "$DRY_RUN" -eq 0 && -n "${FITBIT_CI_SUITE:-}" && "$ROOT" == "$FITBIT_CI_SUITE" ]]; then
    die "refusing to run the gates: the test suite of $ROOT is already running" \
        "fix: a test must point --workspace at a stub repository, not back at $FITBIT_CI_SUITE" \
        "       (the marker is set by scripts/ci/coverage.sh; running the same root inside" \
        "        its own suite would recurse: post_commit.sh -> coverage.sh -> suite -> here)"
fi

cd "$ROOT"
. scripts/_python.sh
commit="$(git rev-parse --short HEAD 2>/dev/null || echo 'not-a-git-checkout')"
workflows="$(ls .github/workflows/*.yml 2>/dev/null | wc -l | tr -d ' ')"
echo "workspace: $ROOT (resolved from $ROOT_SOURCE)"
echo "commit:    $commit"
echo "python:    $PYTHON"
echo "workflow:  $workflows file(s) in $ROOT/.github/workflows"

# name-in-the-summary|workflow file|job id|gate script the job consists of
JOBS=()
if [[ "$WITH_LINT" -eq 1 ]]; then
    JOBS+=("lint|ci.yml|lint|scripts/ci/lint.sh")
fi
JOBS+=("coverage|ci.yml|coverage|scripts/ci/coverage.sh")
if [[ "$QUICK" -eq 0 ]]; then
    JOBS+=("benchmarks|bench.yml|benchmark|scripts/ci/bench.sh")
fi

# --- logs, artifacts, and the reports the summary reads -------------------------
KEEP_LOG_DIR=0
if [[ -n "$LOG_DIR" ]]; then
    KEEP_LOG_DIR=1
    mkdir -p "$LOG_DIR"
else
    LOG_DIR="$(mktemp -d -t fitb-post-commit-XXXXXX)"
fi
[[ -n "$ARTIFACTS" ]] || ARTIFACTS="$LOG_DIR/artifacts"
mkdir -p "$ARTIFACTS"

# The reports the summary block reads are derived artifacts (every one of them is
# gitignored), so dropping them here means a row can only report a number this run
# measured - never the number the previous run left behind.
rm -f coverage.json bench-results.json bench-summary.md

ensure_environment() {
    # A clean clone has no .venv, and scripts/_python.sh would then fall through to
    # whichever python3 is first on PATH - one with none of the dev extra in it, which
    # reports a repository full of import errors instead of a test result (ADR-0001 §12).
    . scripts/_python.sh
    if "$PYTHON" -c 'import coverage, pytest, fitbit_monitor' >/dev/null 2>&1; then
        return 0
    fi
    if [[ "$BOOTSTRAP" -eq 0 ]]; then
        echo "warning: $PYTHON cannot import pytest, coverage or fitbit_monitor and --no-install was passed" >&2
        echo "  fix: ./scripts/dev/bootstrap.sh (or drop --no-install and let this script do it)" >&2
        return 0
    fi
    echo "environment: $PYTHON has no dev extra installed; bootstrapping"
    if command -v uv >/dev/null 2>&1; then
        if uv sync --extra dev --offline >/dev/null 2>&1 || uv sync --extra dev >/dev/null 2>&1; then
            use_bootstrapped_python
            return 0
        fi
    fi
    if ./scripts/dev/bootstrap.sh --pip; then
        use_bootstrapped_python
        return 0
    fi
    echo "error: no environment with the dev extra could be created" >&2
    echo "  fix: run ./scripts/dev/bootstrap.sh (or: uv sync --extra dev) and re-run this" >&2
    return 1
}

use_bootstrapped_python() {
    # Point every gate at the environment that was just created. $PYTHON is already
    # exported by the sourcing above, and scripts/_python.sh deliberately leaves an
    # explicit $PYTHON alone - so the new interpreter is named, not re-derived.
    if [[ -x "$PWD/.venv/bin/python" ]]; then
        PYTHON="$PWD/.venv/bin/python"
        export PYTHON
    fi
    echo "environment: $($PYTHON -V 2>/dev/null || echo '$PYTHON') at $PYTHON"
}

# --- runner selection ----------------------------------------------------------
RUNNER=""
RUNNER_NOTE=""

if [[ "$VIA" != "direct" ]]; then
    echo
    echo "== runner: trying act-mcp (.pi/mcp.json)"
    if "$PYTHON" scripts/ci/_act_mcp.py discover >"$LOG_DIR/act-mcp-discover.log" 2>&1; then
        sed 's/^/  /' "$LOG_DIR/act-mcp-discover.log"
        RUNNER=act-mcp
        RUNNER_NOTE="act-mcp (the server in .pi/mcp.json; the tree it runs is the one it named above)"
    else
        sed 's/^/  /' "$LOG_DIR/act-mcp-discover.log"
        if [[ "$VIA" == "act-mcp" && "$FALLBACK" -eq 0 ]]; then
            die "act-mcp was asked for (--via act-mcp --no-fallback) and is not usable" \
                "fix: start the server configured in .pi/mcp.json, or drop --no-fallback"
        fi
        echo "  act-mcp is not usable here; the gates will run by their scripts instead"
    fi
fi

ACT_BIN="${ACT_BIN:-act}"
if [[ -z "$RUNNER" && "$VIA" != "direct" && "$VIA" != "act-mcp" ]]; then
    echo
    echo "== runner: looking for act ($ACT_BIN) on PATH"
    if command -v "$ACT_BIN" >/dev/null 2>&1; then
        RUNNER=act
        RUNNER_NOTE="act ($($ACT_BIN --version 2>/dev/null || echo 'version unknown')) -W $ROOT --bind, artifact server $ARTIFACTS"
    elif [[ "$VIA" == "act" && "$FALLBACK" -eq 0 ]]; then
        die "no $ACT_BIN binary on PATH (--via act --no-fallback)" \
            "fix: install https://github.com/nektos/act, set ACT_BIN, or drop --no-fallback"
    else
        echo "  no $ACT_BIN on PATH; the gates will run by their scripts instead"
    fi
fi

if [[ -z "$RUNNER" ]]; then
    RUNNER=direct
    RUNNER_NOTE="direct (scripts/ci/coverage.sh, scripts/ci/bench.sh - the command lines the two jobs are)"
fi

echo
echo "runner:    $RUNNER_NOTE"

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo
    echo "== dry run: what would run, in this order"
    for spec in "${JOBS[@]}"; do
        IFS='|' read -r name workflow job gate <<<"$spec"
        case "$RUNNER" in
            act-mcp)
                echo "  $name: $PYTHON scripts/ci/_act_mcp.py run --workspace \"$ROOT\" --workflow $workflow --job $job"
                ;;
            act)
                echo "  $name: $ACT_BIN -W \"$ROOT\" -j $job --bind --artifact-server-path \"$ARTIFACTS\""
                ;;
            *)
                echo "  $name: ./$gate --no-install"
                ;;
        esac
    done
    if [[ "$QUICK" -eq 1 ]]; then
        echo "  benchmarks: skipped (--quick)"
    fi
    exit 0
fi

# --- run one job with the chosen runner ---------------------------------------
run_direct() {
    local gate="$1" log="$2" rc=0
    ensure_environment || return 1
    echo "  \$ ./$gate --no-install  (log: $log)"
    "./$gate" --no-install >"$log" 2>&1 || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        tail -n 3 "$log" | sed 's/^/  | /'
    else
        tail -n 25 "$log" | sed 's/^/  | /'
    fi
    return "$rc"
}

run_act_mcp() {
    local job="$1" workflow="$2" log="$3" rc=0
    "$PYTHON" scripts/ci/_act_mcp.py run \
        --workspace "$ROOT" --workflow "$workflow" --job "$job" --log "$log" \
        >"$log.runner" 2>&1 || rc=$?
    sed 's/^/  | /' "$log.runner"
    return "$rc"
}

run_act() {
    local job="$1" log="$2" rc=0
    echo "  \$ $ACT_BIN -W \"$ROOT\" -j $job --bind --artifact-server-path \"$ARTIFACTS\"  (log: $log)"
    "$ACT_BIN" -W "$ROOT" -j "$job" --bind --artifact-server-path "$ARTIFACTS" >"$log" 2>&1 || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        tail -n 3 "$log" | sed 's/^/  | /'
    else
        tail -n 25 "$log" | sed 's/^/  | /'
    fi
    return "$rc"
}

STATUSES=()
RAN_DIRECT_FOR=()

echo
echo "== running ($RUNNER)"
for spec in "${JOBS[@]}"; do
    IFS='|' read -r name workflow job gate <<<"$spec"
    log="$LOG_DIR/$job.log"
    status=FAIL
    case "$RUNNER" in
        act-mcp)
            if run_act_mcp "$job" "$workflow" "$log"; then
                status=PASS
            elif [[ "$FALLBACK" -eq 1 ]]; then
                echo "  act-mcp did not run $job; running ./$gate in this checkout instead"
                if run_direct "$gate" "$log"; then
                    status=PASS
                    RAN_DIRECT_FOR+=("$name")
                fi
            fi
            ;;
        act)
            if run_act "$job" "$log"; then
                status=PASS
            elif [[ "$FALLBACK" -eq 1 ]]; then
                echo "  act did not run $job; running ./$gate in this checkout instead"
                if run_direct "$gate" "$log"; then
                    status=PASS
                    RAN_DIRECT_FOR+=("$name")
                fi
            fi
            ;;
        *)
            if run_direct "$gate" "$log"; then
                status=PASS
            fi
            ;;
    esac
    if [[ "$status" == PASS ]]; then
        echo "  PASS  $name"
    else
        echo "  FAIL  $name  (job log: $log)"
    fi
    STATUSES+=("$name=$status")
done

if [[ "$QUICK" -eq 1 ]]; then
    STATUSES+=("benchmarks=SKIP")
fi

if [[ "${#RAN_DIRECT_FOR[@]}" -gt 0 ]]; then
    RUNNER_NOTE="$RUNNER_NOTE, except ${RAN_DIRECT_FOR[*]} (run directly because the runner did not run them)"
fi

failed=0
for row in "${STATUSES[@]}"; do
    if [[ "${row##*=}" == FAIL ]]; then
        failed=1
    fi
done
if [[ "$failed" -eq 1 && "$KEEP_LOG_DIR" -eq 0 ]]; then
    echo
    echo "job logs kept in $LOG_DIR"
fi

echo

# One combined block, and its exit status becomes this script's: the workspace that was
# resolved, the runner that actually ran, the coverage percentage, the benchmark
# regression verdict, and a single PASS/FAIL line over all of it.
#
# The block is built and run BEFORE the logs are cleaned up: when act-mcp ran a job, its
# log is the only place the coverage number exists (the reports stayed in the container),
# so deleting the log directory first would throw away the number the block has to print.
summary_args=(
    --workspace "$ROOT"
    --workspace-source "$ROOT_SOURCE"
    --runner "$RUNNER_NOTE"
    --coverage-json coverage.json
    --bench-json bench-results.json
    --bench-summary bench-summary.md
)
[[ -f "$LOG_DIR/coverage.log" ]] && summary_args+=(--coverage-log "$LOG_DIR/coverage.log")
[[ -f "$LOG_DIR/benchmark.log" ]] && summary_args+=(--bench-log "$LOG_DIR/benchmark.log")
for row in "${STATUSES[@]}"; do
    summary_args+=(--gate "$row")
done
summary_rc=0
"$PYTHON" scripts/ci/_gate_summary.py "${summary_args[@]}" || summary_rc=$?

if [[ "$KEEP_LOG_DIR" -eq 0 && "$failed" -eq 0 ]]; then
    rm -rf "$LOG_DIR"
fi
exit "$summary_rc"
