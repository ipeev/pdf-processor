#!/usr/bin/env bash
# One-command Linux bootstrap: checks Docker, keeps existing credentials,
# locks dependencies once, starts services, verifies API+Redis readiness.
set -euo pipefail
cd "$(dirname "$0")/.."

NO_PROMPT=0
if [[ "${1:-}" == "--no-prompt" ]]; then
  NO_PROMPT=1
elif [[ "$#" -gt 0 ]]; then
  echo 'Usage: ./scripts/setup.sh [--no-prompt]' >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo 'Docker is missing. Install Docker Engine and the Compose v2 plugin (see README).' >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo 'Docker Compose v2 plugin is unavailable. Try: docker compose version' >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo 'Cannot access Docker daemon. Check Docker service and your user permissions.' >&2
  echo 'Try: sudo systemctl status docker' >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  (umask 077; cp .env.example .env)
  echo 'Created .env from .env.example'
fi
chmod 600 .env
if ! grep -Eq '^OPENROUTER_API_KEY=[^[:space:]]+' .env; then
  if [[ "$NO_PROMPT" -eq 0 && -t 0 ]]; then
    printf 'OpenRouter API key (input hidden; Enter to skip paid tests): '
    IFS= read -r -s KEY || true
    printf '\n'
    if [[ -n "$KEY" ]]; then
      TMP_ENV="$(mktemp ./.env.XXXXXXXX)"
      chmod 600 "$TMP_ENV"
      trap 'rm -f "$TMP_ENV"' EXIT
      REPLACED=0
      while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == OPENROUTER_API_KEY=* ]]; then
          printf 'OPENROUTER_API_KEY=%s\n' "$KEY" >> "$TMP_ENV"
          REPLACED=1
        else
          printf '%s\n' "$line" >> "$TMP_ENV"
        fi
      done < .env
      if [[ "$REPLACED" -eq 0 ]]; then
        printf 'OPENROUTER_API_KEY=%s\n' "$KEY" >> "$TMP_ENV"
      fi
      mv -f "$TMP_ENV" .env
      trap - EXIT
      unset KEY
      echo 'Saved OpenRouter key in .env (file mode 600).'
    fi
  fi
fi
if ! grep -Eq '^OPENROUTER_API_KEY=[^[:space:]]+' .env; then
  echo 'NOTE: OPENROUTER_API_KEY is empty. Offline tests work, but PDF jobs need it for summaries.'
fi

# Provision a dedicated local ingest secret; never reuse the OpenRouter key.
if ! grep -Eq '^DOCKER_TELEMETRY_TOKEN=[0-9a-f]{64}$' .env; then
  sed -i '/^DOCKER_TELEMETRY_TOKEN=/d' .env
  # Generate a 256-bit token using standard host utilities; setup must not
  # require host Python (application dependencies live inside Docker).
  telemetry_token="$(od -An -N32 -tx1 /dev/urandom | tr -d '[:space:]')"
  if [[ ! "$telemetry_token" =~ ^[0-9a-f]{64}$ ]]; then
    echo 'Unable to generate a secure Docker telemetry token.' >&2
    exit 1
  fi
  printf '\nDOCKER_TELEMETRY_TOKEN=%s\n' "$telemetry_token" >> .env
  unset telemetry_token
fi
if ! grep -Eq '^DOCKER_TELEMETRY_ENABLED=' .env; then
  printf 'DOCKER_TELEMETRY_ENABLED=true\n' >> .env
fi

# OpenSearch requires this host setting; never change the host automatically.
if [[ -r /proc/sys/vm/max_map_count ]]; then
  read -r map_count < /proc/sys/vm/max_map_count
  if [[ ! "$map_count" =~ ^[0-9]+$ ]] || (( map_count < 262144 )); then
    echo 'OpenSearch requires vm.max_map_count >= 262144. Ask the host administrator to configure it.' >&2
    exit 1
  fi
fi

echo 'Checking Docker Compose configuration...'  
docker compose config -q
# Stable release: lockfiles are bundled and MUST be used without resolution.
# uv 0.13.x writes ==3.15.* for the equivalent >=3.15,<3.16 project range.
# Do not silently call PyPI/npm and change verified versions during installation.
backend_version="$(awk -F '"' '/^version = / { print $2; exit }' backend/pyproject.toml)"
backend_requires_python="$(awk -F '"' '/^requires-python = / { print $2; exit }' backend/pyproject.toml)"
backend_lock_version=""
backend_lock_requires_python=""
if [[ -s backend/uv.lock ]]; then
  backend_lock_version="$(awk -F '"' '/^name = "async-pdf-processor"$/ { getline; print $2; exit }' backend/uv.lock)"
  backend_lock_requires_python="$(awk -F '"' '/^requires-python = / { print $2; exit }' backend/uv.lock)"
fi
# uv's canonicalization is semantically identical for this exactly pinned minor.
if [[ "$backend_requires_python" == '>=3.15,<3.16' && "$backend_lock_requires_python" == '==3.15.*' ]]; then
  backend_lock_requires_python="$backend_requires_python"
fi
if [[ -z "$backend_version" || -z "$backend_requires_python" || \
      "$backend_lock_version" != "$backend_version" || \
      "$backend_lock_requires_python" != "$backend_requires_python" ]]; then
  echo 'ERROR: backend/uv.lock is missing or stale; release installation will not re-resolve dependencies.' >&2
  echo 'Use the complete v0.12.8 ZIP. For an intentional dependency update run scripts/lock_dependencies.sh and reverify.' >&2
  exit 1
fi
frontend_version="$(grep -m1 '"version"' frontend/package.json | cut -d '"' -f4)"
frontend_lock_version=""
if [[ -s frontend/package-lock.json ]]; then
  frontend_lock_version="$(grep -m1 '"version"' frontend/package-lock.json | cut -d '"' -f4)"
fi
if [[ -z "$frontend_version" || "$frontend_lock_version" != "$frontend_version" ]]; then
  echo 'ERROR: frontend/package-lock.json missing or stale; release installation will not re-resolve dependencies.' >&2
  echo 'Use the complete v0.12.8 ZIP. For an intentional dependency update run scripts/lock_frontend.sh and reverify.' >&2
  exit 1
fi
echo 'PASS: bundled Python and npm dependency lock versions match their manifests.'

echo 'Starting/updating the application (existing RustFS object volume is preserved)...'
# No implicit teardown or volume deletion. Use docker compose down -v only when
# the operator explicitly wants to discard the entire application state.
echo 'Building and starting services...'
docker compose up --build -d
# A successful image build alone does not prove that previously running
# containers were recreated. Verify the live API and BOTH worker replicas.
# The frontend only contains Nginx assets, so its Compose version label is
# checked instead of trying to read backend Python metadata inside Nginx.
for service in api worker frontend; do
  ids="$(docker compose ps -q --status running "$service")"
  if [[ -z "$ids" ]]; then
    echo "ERROR: No running $service container found after upgrade." >&2
    exit 1
  fi
  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    label_version="$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$container_id")"
    if [[ "$label_version" != "$backend_version" ]]; then
      echo "ERROR: Stale $service container $container_id: expected release label=$backend_version, found=$label_version." >&2
      echo 'After active processing finishes, run: docker compose up -d --build --force-recreate api worker frontend' >&2
      exit 1
    fi
    if [[ "$service" != frontend ]]; then
      if ! running_version="$(docker exec "$container_id" python -c 'import tomllib; from pathlib import Path; print(tomllib.loads(Path("/app/backend/pyproject.toml").read_text())["project"]["version"])')"; then
        echo "ERROR: Could not read the $service container $container_id application version." >&2
        exit 1
      fi
      if [[ "$running_version" != "$backend_version" ]]; then
        echo "ERROR: Stale $service container $container_id: source version=$backend_version, running version=$running_version." >&2
        echo 'After active processing finishes, run: docker compose up -d --build --force-recreate api worker frontend' >&2
        echo 'If the image is still stale, rebuild the application images with --no-cache.' >&2
        echo 'Neither command removes the RustFS or OpenSearch named volumes.' >&2
        exit 1
      fi
    fi
  done <<< "$ids"
done
printf 'PASS: running API, workers and frontend identify release %s.\n' "$backend_version"
printf '\n=== Containers ===\n' 
docker compose ps
printf '\n=== Waiting for API + Redis + RustFS ===\n'
READY=0
for ((attempt=1;attempt<=40;attempt++)); do
  if ./scripts/check_workflow.sh --mode check >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
if [[ "$READY" != 1 ]]; then
  echo 'API/Redis/RustFS readiness failed. Recent logs:' >&2
  docker compose logs --tail=60 api worker redis rustfs >&2
  exit 1
fi
./scripts/check_workflow.sh --mode check

printf '\n=== Permanent archive bucket ===\n'
docker compose exec -T api python -m tests.archive_probe check

printf '\n=== Waiting for internal OpenSearch ===\n'
./scripts/check_opensearch.sh --check
printf '\n=== Catalogue indexes ===\n'
docker compose exec -T api python -m app.catalogue_cli init

printf '\n=== Waiting for React frontend + Nginx API proxy ===\n'
FRONTEND_READY=0
for ((attempt=1;attempt<=40;attempt++)); do
  if ./scripts/check_frontend.sh >/dev/null 2>&1; then
    FRONTEND_READY=1
    break
  fi
  sleep 2
done
if [[ "$FRONTEND_READY" != 1 ]]; then
  echo 'Frontend/proxy readiness failed. Recent logs:' >&2
  docker compose logs --tail=60 frontend api >&2
  exit 1
fi
./scripts/check_frontend.sh

printf '\n=== Docker metrics collector ===\n'
if ! ./scripts/telemetry.sh start; then
  echo 'WARNING: Docker metrics unavailable; PDF processing remains operational.' >&2
fi

printf '\nSetup complete. No PDFs were uploaded during setup.\n'
printf 'Open: http://localhost:3000                    # minimal React frontend\n'
printf 'Run: ./scripts/test_release.sh --offline   # complete no-cost verification\n'
printf 'Run: ./scripts/test_release.sh             # full paid release gate\n'
printf 'Run: ./scripts/check_workflow.sh --mode quick     # 1 paid end-to-end sample\n'
printf 'Run: ./scripts/check_workflow.sh --mode full      # all 3 parsers, paid\n'
printf 'Stop metrics: ./scripts/telemetry.sh stop         # host collector only\n'
