#!/usr/bin/env bash
# Generate or deliberately refresh backend/uv.lock with uv running in the Python 3.15.2 Docker image.
# No host Python/uv installation is required. Re-run after changing pyproject.toml.
set -euo pipefail
cd "$(dirname "$0")/.."
# Never give a bind-mounted project directory to a root-run container.
if [[ "$(id -u)" -eq 0 ]]; then
  echo 'ERROR: run this script as your normal user, not sudo/root.' >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo 'Docker is required. See README.md for Linux setup.' >&2
  exit 1
fi

printf '%s\n' '=== Building uv-enabled Python 3.15.2 image ==='
docker compose build api

printf '%s\n' '=== Resolving and writing backend/uv.lock with uv ==='
# Write the lockfile as the invoking Linux user, not as root. The mounted
# project directory is NOT the app's upload volume.
docker compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -e UV_CACHE_DIR=/tmp/uv-cache \
  --volume "$(pwd)/backend:/src" \
  --workdir /src \
  --entrypoint uv \
  api lock --python /usr/local/bin/python3.15

if [[ ! -s backend/uv.lock ]]; then
  echo 'uv did not create a nonempty backend/uv.lock; aborting.' >&2
  exit 1
fi
printf 'Lockfile written: backend/uv.lock (%s bytes)\n' "$(wc -c < backend/uv.lock)"
printf '%s\n' 'Rebuild the services to install strictly from this lockfile:'
printf '%s\n' '  docker compose up --build -d'
