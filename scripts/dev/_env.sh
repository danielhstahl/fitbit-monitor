#!/usr/bin/env bash
# Shared setup for the scripts/dev entrypoints (ADR-0001 §12).
#
# Resolves the project interpreter and hands it to scripts/ci/*.sh through $PYTHON,
# so that typing `./scripts/dev/test.sh` without activating a virtualenv still runs
# the gates against *this repository's* environment instead of whatever python3 is
# first on PATH. Export, do not assign: the ci scripts read ${PYTHON:-python3}.

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    PYTHON="$VIRTUAL_ENV/bin/python"
elif [[ -x .venv/bin/python ]]; then
    PYTHON="$PWD/.venv/bin/python"
else
    PYTHON="${PYTHON:-python3}"
fi
export PYTHON
