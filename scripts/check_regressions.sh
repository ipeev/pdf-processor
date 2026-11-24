#!/usr/bin/env bash
# Release: targeted regression gate. NO OpenRouter calls. NEVER resets volumes.
set -euo pipefail
cd "$(dirname "$0")/.."
RELEASE_VERSION="$(awk -F '"' '/^version = / {print $2; exit}' backend/pyproject.toml)"
mkdir -p test-results
stamp="$(date +%Y%m%d-%H%M%S)"
log="test-results/v${RELEASE_VERSION}-regressions-${stamp}.log"
echo "v${RELEASE_VERSION} regression gate: uncached frontend build + focused figure, search, retry/cache and progress backend tests"
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
  echo "PASS: v${RELEASE_VERSION} targeted regressions"
} 2>&1 | tee "$log"
