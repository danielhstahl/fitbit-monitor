#!/usr/bin/env bash
# Resolve the interpreter for this repository, in one place (ADR-0001 §12).
#
# Sourced by every script under scripts/ci and scripts/dev. Precedence: an explicit
# $PYTHON (what CI sets), an activated virtualenv, this repository's .venv, python3.
#
# Without it, `./scripts/ci/coverage.sh --no-install` from a fresh clone runs the suite
# against whatever python3 is first on PATH - which has no fitbit_monitor in it, and
# reports a repository full of import errors instead of a test result.

if [[ -n "${PYTHON:-}" ]]; then
    :
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    PYTHON="$VIRTUAL_ENV/bin/python"
elif [[ -x .venv/bin/python ]]; then
    PYTHON="$PWD/.venv/bin/python"
else
    PYTHON=python3
fi
export PYTHON
