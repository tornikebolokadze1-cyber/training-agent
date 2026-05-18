#!/usr/bin/env bash
# Per-file pytest runner that sidesteps issue #52 test pollution.
#
# Background: a handful of test files (test_admin_routes.py,
# test_admin_routes_hardening.py, test_healthz_endpoint.py,
# test_metrics_endpoint.py) need real fastapi / slowapi / httpx / pydantic
# modules instead of the conftest stubs.  They achieve this with a
# module-level ``sys.modules.pop(...)`` at import time.
#
# That pop is permanent for the rest of the pytest session — subsequent
# tests that expected the stubs see the real modules and break.
# 37+ failures in the full-suite run all trace back to this pattern.
# A full architectural fix means refactoring those four test files to
# stop popping at module scope; until that lands, the safe way to verify
# the WHOLE suite is to run each file in its own pytest process so the
# pollution can never cross a file boundary.
#
# Usage:
#   bash scripts/run_tests_isolated.sh            # run everything per-file
#   bash scripts/run_tests_isolated.sh -k unit    # forward extra args to pytest
#
# Exit status:
#   0 — every file passed
#   1 — at least one file failed (the failing files are listed at the end)

set -u

cd "$(dirname "$0")/.." || exit 2

PYTEST_BIN=${PYTEST_BIN:-"python -m pytest"}
EXTRA_ARGS=("$@")

declare -a FAILED_FILES=()
TOTAL=0
PASSED=0

# Collect test files in deterministic order so the report is reproducible.
mapfile -t TEST_FILES < <(find tools/tests -name "test_*.py" -type f | sort)

if [ "${#TEST_FILES[@]}" -eq 0 ]; then
    echo "No test files found under tools/tests/." >&2
    exit 2
fi

for test_file in "${TEST_FILES[@]}"; do
    TOTAL=$((TOTAL + 1))
    printf '\n──── [%2d/%2d] %s ────\n' "$TOTAL" "${#TEST_FILES[@]}" "$test_file"
    if $PYTEST_BIN "$test_file" -x -q "${EXTRA_ARGS[@]}"; then
        PASSED=$((PASSED + 1))
    else
        FAILED_FILES+=("$test_file")
    fi
done

echo
echo "════════════════════════════════════════════════════════════════"
printf 'Total files: %d   Passed: %d   Failed: %d\n' \
    "$TOTAL" "$PASSED" "${#FAILED_FILES[@]}"
echo "════════════════════════════════════════════════════════════════"

if [ "${#FAILED_FILES[@]}" -gt 0 ]; then
    echo
    echo "Failing files:"
    for f in "${FAILED_FILES[@]}"; do
        echo "  - $f"
    done
    exit 1
fi

exit 0
