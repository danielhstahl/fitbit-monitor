#!/usr/bin/env bash
# Install or remove the opt-in git hooks that live in this directory.
#
# The gate trio (type check, coverage, benchmarks) has to run after every commit - that
# is this repository's AGENTS.md rule, and the reason scripts/ci/post_commit.sh exists.
# This script is the opt-in that makes git call it: it symlinks scripts/git-hooks/
# post-commit into the clone's hooks directory, which is the two-line version of the
# same idea that the pre-commit framework turns into a dependency (ADR-0001 §12).
#
# Usage:
#   ./scripts/git-hooks/install.sh            # install every hook in this directory
#   ./scripts/git-hooks/install.sh --remove   # remove the ones this script installed
#   ./scripts/git-hooks/install.sh --status   # what is installed where, and whether it is ours
#   ./scripts/git-hooks/install.sh --force    # replace a hook that is not ours
#
# If you would rather git look in the working tree for hooks - one command, nothing
# symlinked, and it survives a moved clone - this works instead and needs no script:
#
#   git config core.hooksPath scripts/git-hooks
#
# That has one downside worth naming: core.hooksPath applies to every hook in the clone,
# so a hook someone else put in .git/hooks stops running. docs/ci.md has both options.
set -euo pipefail

usage() {
    sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '/^set -euo pipefail/d; s/^# \{0,1\}//'
}

ROOT=""
REMOVE=0
STATUS_ONLY=0
FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --remove)
            REMOVE=1
            shift
            ;;
        --status)
            STATUS_ONLY=1
            shift
            ;;
        --force)
            FORCE=1
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

if ! root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    echo "error: not inside a git repository, so there is no hooks directory to write to" >&2
    echo "  fix: run this from a clone of fitbit-monitor (git rev-parse --show-toplevel must succeed)" >&2
    exit 2
fi
ROOT="$root"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$here" != "$ROOT/scripts/git-hooks" ]]; then
    echo "error: this script expects to live at $ROOT/scripts/git-hooks (found $here)" >&2
    echo "  fix: run it from the clone: $ROOT/scripts/git-hooks/install.sh" >&2
    exit 2
fi

if ! hooks_dir="$(git rev-parse --git-path hooks)"; then
    echo "error: git could not tell us where this clone's hooks live" >&2
    exit 2
fi
[[ "$hooks_dir" = /* ]] || hooks_dir="$PWD/$hooks_dir"
if [[ ! -d "$hooks_dir" ]]; then
    mkdir -p "$hooks_dir"
fi
hooks_dir="$(cd "$hooks_dir" && pwd)"

mark="installed by scripts/git-hooks/install.sh"
status=0

for source in "$here"/*; do
    name="$(basename "$source")"
    [[ "$name" == "install.sh" ]] && continue
    [[ -f "$source" ]] || continue
    chmod +x "$source"
    target="$hooks_dir/$name"

    if [[ "$STATUS_ONLY" -eq 1 ]]; then
        if [[ -L "$target" && "$(readlink "$target")" == "$source" ]]; then
            echo "ok       $name -> $source"
        elif [[ -e "$target" ]]; then
            echo "foreign  $target exists and is not this file (not managed here)"
        else
            echo "absent   $name: not installed"
        fi
        continue
    fi

    if [[ "$REMOVE" -eq 1 ]]; then
        if [[ -L "$target" && "$(readlink "$target")" == "$source" ]]; then
            rm -f "$target"
            echo "removed  $target"
        elif [[ -e "$target" ]]; then
            echo "left     $target is not the hook this script installed; not touching it" >&2
            status=1
        else
            echo "absent   $name was not installed"
        fi
        continue
    fi

    if [[ -e "$target" || -L "$target" ]]; then
        if [[ "$(readlink "$target" 2>/dev/null || true)" == "$source" ]]; then
            echo "ok       $target already points here"
            continue
        fi
        if [[ "$FORCE" -eq 0 ]]; then
            echo "refused  $target already exists and is not $source" >&2
            echo "         fix: remove it, or re-run with --force to replace it" >&2
            status=1
            continue
        fi
        cp "$target" "$target.before-git-hooks.$(date +%s)"
        echo "backed   $target up next to itself"
    fi

    ln -s "$source" "$target"
    echo "installed $name -> $source ($mark)"
done

if [[ "$STATUS_ONLY" -eq 0 && "$REMOVE" -eq 0 ]]; then
    cat <<NEXT

next:
  git commit --allow-empty -m 'probe: does the post-commit gate run?'   # then delete it
  POST_COMMIT_ARGS=--quick git commit ...        # coverage only, no benchmark
  POST_COMMIT_SKIP=1 git commit ...             # once, without the gates
  $ROOT/scripts/git-hooks/install.sh --status   # what is wired up
NEXT
fi
exit "$status"
