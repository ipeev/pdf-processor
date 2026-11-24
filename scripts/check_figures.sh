#!/usr/bin/env bash
# v0.12.6: offline figure gate (default), or ONE explicitly paid end-to-end smoke.
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:---offline}"
if [[ $# -gt 1 || "$mode" != --offline && "$mode" != --paid-live ]]; then
  echo 'Usage: ./scripts/check_figures.sh [--offline|--paid-live]' >&2
  exit 2
fi
mkdir -p test-results
stamp="$(date +%Y%m%d-%H%M%S)"
log="test-results/v0.12.6-figures-${mode#--}-${stamp}.log"
if [[ "$mode" == --offline ]]; then
  echo 'v0.12.6: required REAL RustFS, Redis and OpenSearch figure integration; saved model replies; no paid calls.'
  set +e
  docker compose exec -T api uv run --locked --extra test pytest -q \
    -m pipeline_integration -rs tests/test_figures_release.py 2>&1 | tee "$log"
  code=${PIPESTATUS[0]}
  set -e
  if [[ "$code" -ne 0 ]] || ! grep -Eq '[1-9][0-9]* passed' "$log" || grep -Eq '[1-9][0-9]* skipped' "$log"; then
    echo "FAIL: figure integration must PASS with no skips; full log: $log" >&2
    exit 1
  fi
  echo "PASS: real-service figure integration; $log"
else
  echo 'Explicit paid test selected: 2-page PDF, up to 2 Haiku page calls plus summary provider calls.'
  echo 'No automatic smoke-upload retry. Read provider privacy and costs before running.'
  set +e
  docker compose exec -T api uv run --locked python /app/scripts/check_figures.py \
    --paid-live 2>&1 | tee "$log"
  code=${PIPESTATUS[0]}
  set -e
  # Extract structured non-secret report, even for failed tests.
  sed -n 's/^FIGURE_SMOKE_JSON=//p' "$log" | tail -n 1 > "${log%.log}.json"
  [[ "$code" == 0 ]] || { echo "FAIL: $log (+ JSON report); do not automatically re-upload" >&2; exit "$code"; }
  echo "PASS: $log (+ JSON report)"
fi
