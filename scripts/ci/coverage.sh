#!/usr/bin/env bash
# Code coverage gate for fitbit-monitor.
#
# This is the gate; `.github/workflows/ci.yml` only calls it, which is what keeps a
# laptop and a runner running the same command and failing the same way (AGENTS.md:
# the action holds no logic of its own).
#
# The last thing it prints is one machine-readable line - `coverage: NN.N%` - plus a
# line per package and a line naming each artifact, all produced by
# scripts/ci/_coverage_report.py, which is where the floor arithmetic lives and what
# tests/test_ci_coverage_gate.py tests directly:
#
#   coverage: 92.0%
#   coverage overall 92.0% (floor 85.0%) ok
#   coverage package api 94.2% (floor 90.0%) ok
#   artifact coverage-xml: coverage.xml
#   artifact coverage-json: coverage.json
#
# The floors themselves are committed in pyproject.toml - `[tool.coverage.report]
# fail_under` overall (the same number `coverage report` enforces) and
# `[tool.fitbit_monitor.coverage]` per package - so there is one place to change and
# nothing to keep in sync between YAML and shell. The flags below override them for a
# single run, which is how a ratchet gets tried before it is committed; nothing here
# is a way to run a push below the committed floor.
#
# Usage: scripts/ci/coverage.sh [--no-install] [options]
#   --no-install                skip the `pip install -e ".[dev]"` step (CI passes this)
#   --floor FLOAT               overall floor, overriding pyproject
#   --package-floor NAME=FLOAT  one package's floor, overriding pyproject; repeatable
#   --default-package-floor FLOAT  floor for packages with no committed entry
#   --xml-out PATH              XML report to write        (default coverage.xml)
#   --json-out PATH             JSON report to write       (default coverage.json)
#   --artifact-name NAME        artifact name for the XML  (default coverage-xml)
#   --json-artifact-name NAME   artifact name for the JSON (default coverage-json)
#   -h, --help                  this text
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
. scripts/_python.sh

FLOOR=""
PACKAGE_FLOORS=()
DEFAULT_PACKAGE_FLOOR=""
XML_OUT=coverage.xml
JSON_OUT=coverage.json
ARTIFACT_NAME=coverage-xml
JSON_ARTIFACT_NAME=coverage-json
INSTALL=1

usage() {
    sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '/^set -euo pipefail/d; s/^# \{0,1\}//'
}

# Accepts both `--flag value` and `--flag=value`, so the workflow and a hand-typed
# command line look the same from here.
need_value() {
    if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "error: $1 needs a value (see --help)" >&2
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-install)
            INSTALL=0
            shift
            ;;
        --floor)
            need_value "$@"
            FLOOR="${2:-}"
            shift 2
            ;;
        --floor=*)
            FLOOR="${1#*=}"
            shift
            ;;
        --package-floor)
            need_value "$@"
            PACKAGE_FLOORS+=("${2:-}")
            shift 2
            ;;
        --package-floor=*)
            PACKAGE_FLOORS+=("${1#*=}")
            shift
            ;;
        --default-package-floor)
            need_value "$@"
            DEFAULT_PACKAGE_FLOOR="${2:-}"
            shift 2
            ;;
        --default-package-floor=*)
            DEFAULT_PACKAGE_FLOOR="${1#*=}"
            shift
            ;;
        --xml-out)
            need_value "$@"
            XML_OUT="${2:-}"
            shift 2
            ;;
        --xml-out=*)
            XML_OUT="${1#*=}"
            shift
            ;;
        --json-out)
            need_value "$@"
            JSON_OUT="${2:-}"
            shift 2
            ;;
        --json-out=*)
            JSON_OUT="${1#*=}"
            shift
            ;;
        --artifact-name)
            need_value "$@"
            ARTIFACT_NAME="${2:-}"
            shift 2
            ;;
        --artifact-name=*)
            ARTIFACT_NAME="${1#*=}"
            shift
            ;;
        --json-artifact-name)
            need_value "$@"
            JSON_ARTIFACT_NAME="${2:-}"
            shift 2
            ;;
        --json-artifact-name=*)
            JSON_ARTIFACT_NAME="${1#*=}"
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

# Fail here, with a sentence, rather than 300 lines of ModuleNotFoundError from
# whichever interpreter happened to be first on PATH (ADR-0001 §12).
"$PYTHON" -c 'import re, sys, tomllib, pathlib

spec = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["requires-python"]
floor = tuple(int(part) for part in re.findall(r"\d+", spec)[:2])
if sys.version_info[:2] < floor:
    print(f"error: {sys.version.split()[0]} is below requires-python {spec}", file=sys.stderr)
    print(f"  fix: use one of the interpreters listed in .python-version ({sys.version_info[:2]})", file=sys.stderr)
    raise SystemExit(2)
print(f"python {sys.version.split()[0]} (requires {spec})")
'

if [[ "$INSTALL" -eq 1 ]]; then
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -e ".[dev]"
fi

echo
echo "== test suite (unittest, so pytest is optional)"
"$PYTHON" -m unittest discover -s tests -t tests -q

echo
echo "== coverage"
rm -f .coverage
"$PYTHON" -m coverage run --branch --source=src/fitbit_monitor -m pytest -q tests
"$PYTHON" -m coverage report

echo
echo "== reports"
"$PYTHON" -m coverage xml -o "$XML_OUT"
echo "wrote $XML_OUT"
"$PYTHON" -m coverage json -o "$JSON_OUT"
echo "wrote $JSON_OUT"

echo
echo "== floors (pyproject.toml; --help for the overrides)"
FLOOR_ARGS=()
[[ -n "$FLOOR" ]] && FLOOR_ARGS+=(--floor "$FLOOR")
[[ -n "$DEFAULT_PACKAGE_FLOOR" ]] && FLOOR_ARGS+=(--default-package-floor "$DEFAULT_PACKAGE_FLOOR")
for entry in "${PACKAGE_FLOORS[@]+"${PACKAGE_FLOORS[@]}"}"; do
    FLOOR_ARGS+=(--package-floor "$entry")
done
exec "$PYTHON" scripts/ci/_coverage_report.py \
    --json "$JSON_OUT" \
    --xml-out "$XML_OUT" \
    --json-out "$JSON_OUT" \
    --artifact-name "$ARTIFACT_NAME" \
    --json-artifact-name "$JSON_ARTIFACT_NAME" \
    "${FLOOR_ARGS[@]+"${FLOOR_ARGS[@]}"}"
