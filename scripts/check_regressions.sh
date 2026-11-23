#!/usr/bin/env bash
# v0.12.3: targeted regression gate. NO OpenRouter calls. NEVER resets volumes.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p test-results
stamp="$(date +%Y%m%d-%H%M%S)"
log="test-results/v0.12.3-regressions-${stamp}.log"
echo 'v0.12.3 regression gate: uncached frontend npm test/build + focused figure backend tests'
echo "Log: $log"
{
  echo '--- Uncached frontend container build (catches missing named imports and full TS/Vite build failures) ---'
  docker compose build --no-cache frontend
  echo '--- Vision figures Python regression suite, no provider calls ---'
  docker compose exec -T api uv run --locked --extra test python -m pytest -q \
    tests/test_vision_figures_alpha.py tests/test_vision_figures_caption_clearance.py
  echo 'PASS: v0.12.3 targeted regressions'
} 2>&1 | tee "$log"
