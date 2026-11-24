#!/usr/bin/env bash
# v0.12.6: targeted regression gate. NO OpenRouter calls. NEVER resets volumes.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p test-results
stamp="$(date +%Y%m%d-%H%M%S)"
log="test-results/v0.12.6-regressions-${stamp}.log"
echo 'v0.12.6 regression gate: uncached frontend build + focused figure, search, retry/cache and progress backend tests'
echo "Log: $log"
{
  echo '--- Uncached frontend container build (catches missing named imports and full TS/Vite build failures) ---'
  docker compose build --no-cache frontend
  echo '--- Vision figures Python regression suite, no provider calls ---'
  docker compose exec -T api uv run --locked --extra test python -m pytest -q \
    tests/test_vision_figures_alpha.py tests/test_vision_figures_caption_clearance.py \
    tests/test_vision_retry_cache.py tests/test_vision_response_logging.py \
    tests/test_page_cache.py tests/test_page_progress.py \
    tests/test_phase4_cache.py tests/test_fulltext_search.py
  echo 'PASS: v0.12.6 targeted regressions'
} 2>&1 | tee "$log"
