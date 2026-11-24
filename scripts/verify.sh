#!/usr/bin/env bash
# Standard tests are offline + real Redis. Paid checks require explicit flags.
# --live: model contract probes; --quick: one end-to-end PDF; --full: all samples.
set -euo pipefail
cd "$(dirname "$0")/.."

LIVE=0
WORKFLOW=''
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    --quick) WORKFLOW=quick ;;
    --full) WORKFLOW=full ;;
    *) echo "Unknown option: $arg. Usage: $0 [--live] [--quick|--full]" >&2; exit 2 ;;
  esac
done
# Validate release metadata, docs and workflow in the complete source checkout.
# README/docs/.github are deliberately excluded from backend runtime images.
printf '\n=== Full source release and documentation consistency ===\n'
python3 scripts/check_release_candidate.py
./scripts/setup.sh --no-prompt

printf '\n=== Locked Python environment ===\n'
docker compose exec -T api uv --version
docker compose exec -T api uv run --locked --extra test python --version
docker compose exec -T api uv run --locked --extra test python -c 'import sys; assert sys.version_info[:3] == (3, 15, 2), sys.version'
docker compose exec -T worker python -c 'import sys; assert sys.version_info[:3] == (3, 15, 2), sys.version'
docker compose exec -T api uv lock --check
printf '\n=== v0.12.7: bundled lockfile integrity ===\n'
docker compose exec -T api python /app/scripts/check_release_candidate.py --runtime-only
printf '\n=== Docker image project layout ===\n'
if ! docker compose exec -T api sh -c \
  'test -f /app/backend/pyproject.toml && test -f /app/backend/tests/test_workflow_cli.py && test -f /app/scripts/check_workflow.py && test -f /app/docker-compose.yml'; then
  echo 'ERROR: Docker image is missing repository files. Rebuild with: docker compose up --build -d' >&2
  exit 1
fi
printf 'PASS: repository and test script paths exist inside Docker\n'
printf '\n=== Unprivileged application runtime ===\n'
for service in api worker; do
  runtime_uid="$(docker compose exec -T "$service" id -u | tr -d '\r')"
  if [[ "$runtime_uid" != "10001" ]]; then
    echo "FAIL: $service must run as UID 10001, got $runtime_uid" >&2
    exit 1
  fi
  printf 'PASS: %s runs as UID %s (not root)\n' "$service" "$runtime_uid"
done
# Docker Compose main services must not bind-mount a host project directory.
# The test runner's frontend mount is read-only; the benchmark wrapper uses host UID/GID.

printf '\n=== Two independent Docker worker containers ===\n'
worker_count="$(docker compose ps -q --status running worker | sed '/^$/d' | wc -l | tr -d ' ')"
if [[ "$worker_count" -lt 2 ]]; then
  echo "FAIL: two running workers required, found $worker_count" >&2
  exit 1
fi
printf 'PASS: %s workers running\n' "$worker_count"

# Validate *effective* Docker Engine limits, not just Compose YAML. `mem_limit`
# applies individually to every scaled worker replica. No container has a
# reserved slice of host RAM; these are independent hard maximums.
printf '\n=== Per-container Docker memory limits ===\n'
for policy in 'worker:4294967296:2' 'api:2147483648:1' \
              'frontend:2147483648:1' 'redis:2147483648:1' 'rustfs:2147483648:1' 'opensearch:2147483648:1'; do
  IFS=: read -r service expected_bytes expected_count <<< "$policy"
  ids="$(docker compose ps -q --status running "$service")"
  count="$(printf '%s\n' "$ids" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [[ "$count" -ne "$expected_count" ]]; then
    echo "FAIL: $service expected $expected_count running container(s), got $count" >&2
    exit 1
  fi
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    actual_bytes="$(docker inspect --format '{{.HostConfig.Memory}}' "$id")"
    if [[ "$actual_bytes" != "$expected_bytes" ]]; then
      echo "FAIL: $service ($id) has $actual_bytes bytes of Docker memory limit; expected $expected_bytes" >&2
      exit 1
    fi
  done <<< "$ids"
  printf 'PASS: %s: %s container(s), limit %s bytes each\n' "$service" "$count" "$expected_bytes"
done

printf '\n=== RC.1: effective host network bindings ===\n'
./scripts/check_release_surface.sh
printf '\n=== Stage 7: cross-container archive coordination ===\n'
bash scripts/check_archive_coordination.sh

printf '\n=== Effective Docker log rotation ===\n'
bash scripts/check_log_rotation.sh
printf '\n=== Complete pytest suite (includes real Redis tests) ===\n'
# The production API/worker image intentionally excludes React source files.
# A development installer may set umask 077, leaving frontend/ inaccessible to
# container UID 10001. Stage only non-secret frontend test inputs under /tmp,
# readable by the non-root test container. Do not chmod the installed project,
# mount credentials, or run tests as root.
test_frontend="$(mktemp -d "${TMPDIR:-/tmp}/pdf-processor-frontend-tests.XXXXXXXX")"
cleanup_test_frontend() { rm -rf -- "$test_frontend"; }
trap cleanup_test_frontend EXIT
cp -a -- frontend/Dockerfile frontend/nginx.conf frontend/package.json frontend/package-lock.json \
  frontend/src frontend/tests "$test_frontend/"
chmod -R a+rX -- "$test_frontend"
docker compose run --rm -T --no-deps \
  -v "$test_frontend:/app/frontend:ro" \
  api uv run --locked --extra test pytest -q
cleanup_test_frontend
trap - EXIT
printf '\n=== Required real Redis integration tests (no skips accepted) ===\n'
# These must actually run; a skipped integration suite is NOT a release pass.
if ! REDIS_INTEGRATION_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q -m redis_integration -rs tests/test_redis_integration.py 2>&1)"; then
  printf '%s\n' "$REDIS_INTEGRATION_REPORT"
  echo 'FAIL: pytest failed in this required integration suite.' >&2
  exit 1
fi
printf '%s\n' "$REDIS_INTEGRATION_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$REDIS_INTEGRATION_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$REDIS_INTEGRATION_REPORT"; then
  echo 'FAIL: real Redis integration tests must all execute and pass.' >&2
  exit 1
fi
printf '\n=== Phase 2: Redis worker takeover (must not skip) ===\n'
if ! PHASE2_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q \
  -m redis_integration tests/test_phase2_failover.py -rs 2>&1)"; then
  printf '%s\n' "$PHASE2_REPORT"
  echo 'FAIL: pytest failed in this required integration suite.' >&2
  exit 1
fi
printf '%s\n' "$PHASE2_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$PHASE2_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$PHASE2_REPORT"; then
  echo 'FAIL: real Redis worker takeover tests must execute and pass.' >&2
  exit 1
fi
printf '\n=== Phase 3: real RustFS S3 integration (no skips accepted) ===\n'
if ! S3_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q \
  -m s3_integration tests/test_phase3_object_store.py -rs 2>&1)"; then
  printf '%s\n' "$S3_REPORT"
  echo 'FAIL: pytest failed in this required integration suite.' >&2
  exit 1
fi
printf '%s\n' "$S3_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$S3_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$S3_REPORT"; then
  echo 'FAIL: real S3 integration tests must execute and pass.' >&2
  exit 1
fi
printf '\n=== Stage 2: real RustFS permanent archive (no skips accepted) ===\n'
if ! ARCHIVE_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q \
  -m s3_integration tests/test_archive_store.py -rs 2>&1)"; then
  printf '%s\n' "$ARCHIVE_REPORT"
  echo 'FAIL: real archive integration failed; do not enable archival processing.' >&2
  exit 1
fi
printf '%s\n' "$ARCHIVE_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$ARCHIVE_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$ARCHIVE_REPORT"; then
  echo 'FAIL: permanent archive integration tests must all execute and pass.' >&2
  exit 1
fi
printf '\n=== Stage 3: required real catalogue and index reconstruction ===\n'
if ! CATALOGUE_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q -m opensearch_integration tests/test_catalogue.py -rs 2>&1)"; then
  printf '%s\n' "$CATALOGUE_REPORT"
  echo 'FAIL: real catalogue integration failed.' >&2
  exit 1
fi
printf '%s\n' "$CATALOGUE_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$CATALOGUE_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$CATALOGUE_REPORT"; then
  echo 'FAIL: real catalogue integration must execute and pass.' >&2
  exit 1
fi
printf '\n=== v0.12.7: required real OpenSearch BM25 passage search (no paid calls) ===\n'
if ! SEARCH_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q -m opensearch_integration tests/test_fulltext_search_integration.py -rs 2>&1)"; then
  printf '%s\n' "$SEARCH_REPORT"
  echo 'FAIL: real full-text search integration failed.' >&2
  exit 1
fi
printf '%s\n' "$SEARCH_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$SEARCH_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$SEARCH_REPORT"; then
  echo 'FAIL: real BM25 and document-collapse tests must execute and pass.' >&2
  exit 1
fi
printf '\n=== Stage 4: real persistent pipeline (mocked AI, no skips accepted) ===\n'
if ! PIPELINE_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q -m pipeline_integration tests/test_persistent_pipeline.py -rs 2>&1)"; then
  printf '%s\n' "$PIPELINE_REPORT"
  echo 'FAIL: persistent pipeline integration failed.' >&2
  exit 1
fi
printf '%s\n' "$PIPELINE_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$PIPELINE_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$PIPELINE_REPORT"; then
  echo 'FAIL: persistent pipeline integration must execute and pass.' >&2
  exit 1
fi
printf '\n=== Stage 5: real persistent HTTP API (mocked AI, no skips accepted) ===\n'
if ! API_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q -m pipeline_integration tests/test_library_api.py -rs 2>&1)"; then
  printf '%s\n' "$API_REPORT"
  echo 'FAIL: persistent API integration failed.' >&2
  exit 1
fi
printf '%s\n' "$API_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$API_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$API_REPORT"; then
  echo 'FAIL: persistent API integration must execute and pass.' >&2
  exit 1
fi
printf '\n=== Stage 7: real archive maintenance (mocked AI, no skips accepted) ===\n'
if ! MAINTENANCE_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q -m pipeline_integration tests/test_archive_maintenance.py -rs 2>&1)"; then
  printf '%s\n' "$MAINTENANCE_REPORT"
  echo 'FAIL: archive maintenance integration failed.' >&2
  exit 1
fi
printf '%s\n' "$MAINTENANCE_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$MAINTENANCE_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$MAINTENANCE_REPORT"; then
  echo 'FAIL: archive maintenance integration must execute and pass.' >&2
  exit 1
fi
printf '\n=== Stage 8: real archive operations and monitoring (mocked AI, no skips accepted) ===\n'
if ! OPERATIONS_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q -m pipeline_integration tests/test_archive_operations.py -rs 2>&1)"; then
  printf '%s\n' "$OPERATIONS_REPORT"
  echo 'FAIL: archive operations integration failed.' >&2
  exit 1
fi
printf '%s\n' "$OPERATIONS_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$OPERATIONS_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$OPERATIONS_REPORT"; then
  echo 'FAIL: archive operations integration must execute and pass.' >&2; exit 1
fi
printf '\n=== Phase 4: real Redis SHA-256 caching (no skips accepted) ===\n'
if ! CACHE_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q \
  -m redis_integration tests/test_phase4_cache.py -rs 2>&1)"; then
  printf '%s\n' "$CACHE_REPORT"
  echo 'FAIL: pytest failed in this required integration suite.' >&2
  exit 1
fi
printf '%s\n' "$CACHE_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$CACHE_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$CACHE_REPORT"; then
  echo 'FAIL: real Redis cache tests must execute and pass.' >&2
  exit 1
fi
printf '\n=== Phase 5.5: real Redis system monitoring (no skips accepted) ===\n'
if ! MONITOR_REPORT="$(docker compose exec -T api uv run --locked --extra test pytest -q \
  -m redis_integration tests/test_system_monitor.py -rs 2>&1)"; then
  printf '%s\n' "$MONITOR_REPORT"
  echo 'FAIL: system monitoring integration test failed.' >&2
  exit 1
fi
printf '%s\n' "$MONITOR_REPORT"
if ! grep -Eq '[1-9][0-9]* passed' <<< "$MONITOR_REPORT" || grep -Eq '[1-9][0-9]* skipped' <<< "$MONITOR_REPORT"; then
  echo 'FAIL: monitoring Redis integration must run and pass.' >&2
  exit 1
fi
printf '\n=== Stage 9: real, isolated Redis restart recovery (no AI, no production Redis restart) ===\n'
./scripts/check_redis_restart_recovery.sh
printf '\n=== RustFS temporary and permanent objects survive restart ===\n'
./scripts/check_storage_restart.sh
printf '\n=== OpenSearch integration and restart persistence ===\n'
./scripts/check_opensearch.sh --restart
printf '\n=== v0.12.7: REQUIRED live-service vision-figures gate (saved OpenRouter fixtures; NO PAID CALLS) ===\n'
./scripts/check_figures.sh --offline
printf '\n=== React frontend + same-origin FastAPI proxy ===\n'
./scripts/check_frontend.sh
printf 'PASS: frontend unit tests and TypeScript build ran in Docker build stage.\n'

printf '\n=== Redis Streams ===\n'
docker compose exec -T redis redis-cli PING
printf '\n=== Temporary storage policy ===\n'
# Verify runtime Redis config, not just the Compose file.
if [[ "$(docker compose exec -T redis redis-cli --raw CONFIG GET appendonly | tail -n 1 | tr -d '\r')" != 'no' ]]; then
  echo 'FAIL Redis AOF must be disabled.' >&2; exit 1
fi
if [[ -n "$(docker compose exec -T redis redis-cli --raw CONFIG GET save | tail -n 1 | tr -d '\r')" ]]; then
  echo 'FAIL Redis RDB snapshots must be disabled.' >&2; exit 1
fi
if [[ "$(docker compose exec -T redis redis-cli --raw CONFIG GET maxmemory-policy | tail -n 1 | tr -d '\r')" != 'noeviction' ]]; then
  echo 'FAIL Redis must not silently evict in-flight jobs.' >&2; exit 1
fi
if [[ "$(docker compose exec -T redis redis-cli --raw CONFIG GET maxmemory | tail -n 1 | tr -d '\r')" != '1610612736' ]]; then
  echo 'FAIL Redis maxmemory should be 1536 MiB (1610612736 bytes).' >&2; exit 1
fi
printf 'PASS Redis is volatile, capped, and uses noeviction.\n'
# Inspect the actual configured stream, not a hardcoded default.
STREAM_KEY="$(docker compose exec -T worker sh -c 'printf "%s" "${STREAM_KEY:-documents:jobs}"')"
STREAM_READY=0
for ((attempt=1;attempt<=20;attempt++)); do
  if docker compose exec -T redis redis-cli XINFO GROUPS "$STREAM_KEY" >/dev/null 2>&1; then
    STREAM_READY=1; break
  fi
  sleep 1
done
if [[ "$STREAM_READY" != 1 ]]; then
  docker compose logs --tail=50 worker >&2
  echo 'Worker did not create Redis consumer group.' >&2
  exit 1
fi
docker compose exec -T redis redis-cli XINFO GROUPS "$STREAM_KEY"

if [[ "$LIVE" -eq 1 || -n "$WORKFLOW" ]]; then
  if ! docker compose exec -T worker sh -c 'test -n "$OPENROUTER_API_KEY"'; then
    echo 'Paid tests requested but OPENROUTER_API_KEY is empty in the worker container.' >&2
    exit 1
  fi
fi
if [[ "$LIVE" -eq 1 ]]; then
  printf '\n=== Paid OpenRouter model probes ===\n'
  docker compose exec -T -e RUN_LIVE_TESTS=1 api \
    uv run --locked --extra test pytest -q -m live tests/test_live_openrouter.py
fi
if [[ -n "$WORKFLOW" ]]; then
  printf '\n=== Paid HTTP end-to-end workflow: %s ===\n' "$WORKFLOW"
  ./scripts/check_workflow.sh --mode "$WORKFLOW"
fi
printf '\nPASS: requested verification finished successfully.\n'
