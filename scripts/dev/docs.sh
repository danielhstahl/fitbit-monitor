#!/usr/bin/env bash
# Regenerate the machine-written half of docs/plugins.md (ADR-0001 §12).
#
# The API reference block in that document is generated from the plugin contract, and
# tests/test_docs.py fails when it drifts - so this is the command to run after
# touching fitbit_monitor/plugins/contract.py, not a place to make new decisions.
#
# Usage: ./scripts/dev/docs.sh           # rewrite the block in place
#        ./scripts/dev/docs.sh --check  # exit nonzero if the block is stale
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

if [[ "${1:-}" == "--check" ]]; then
    exec "$PYTHON" scripts/docs/api_reference.py --check
fi
exec "$PYTHON" scripts/docs/api_reference.py --write
