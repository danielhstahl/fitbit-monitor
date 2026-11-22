#!/usr/bin/env bash
# Create the development environment, idempotently (ADR-0001 §3, §12).
#
# uv is preferred when it is on PATH - it reads .python-version and uv.lock, so a
# fresh clone gets exactly the tool versions CI installs. Without uv, this falls back
# to `python3 -m venv` + pip, which is the path documented for people who refuse to
# install a new tool to read their own health data.
#
# Usage:
#   ./scripts/dev/bootstrap.sh            # uv sync --extra dev, or venv + pip
#   ./scripts/dev/bootstrap.sh --clean    # throw .venv away first
#   ./scripts/dev/bootstrap.sh --pip      # ignore uv, use venv + pip
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

CLEAN=0
USE_UV=1
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        --pip) USE_UV=0 ;;
        *) echo "usage: $0 [--clean] [--pip]" >&2; exit 2 ;;
    esac
done

if [[ "$CLEAN" -eq 1 ]]; then
    echo "removing .venv"
    rm -rf .venv
fi

if [[ "$USE_UV" -eq 1 ]] && command -v uv >/dev/null 2>&1; then
    echo "== uv bootstrap (uv.lock: $(wc -l < uv.lock 2>/dev/null || echo 'missing') lines)"
    uv sync --extra dev
else
    echo "== venv + pip bootstrap"
    "${PYTHON:-python3}" -m venv .venv
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -e ".[dev]"
fi

. scripts/_python.sh
echo
echo "environment: $("$PYTHON" -V) at $PYTHON"
"$PYTHON" -c "import fitbit_monitor, sys; print('fitbit-monitor', fitbit_monitor.__version__, 'from', fitbit_monitor.__file__)"
cat <<'NEXT'

next:
  ./scripts/dev/check.sh            # every gate CI runs, the way CI runs them
  ./scripts/dev/test.sh             # tests + coverage floor only
NEXT
