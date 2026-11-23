#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Never give a bind-mounted project directory to a root-run container.
if [[ "$(id -u)" -eq 0 ]]; then
  echo 'ERROR: run this script as your normal user, not sudo/root.' >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo 'ERROR: Docker required. See benchmarks/README.md for Python alternative.' >&2
  exit 2
fi
if [[ $# -eq 0 ]]; then
  echo 'Usage: ./scripts/benchmark.sh {all|generate|run|evaluate|test} [arguments...]' >&2
  exit 2
fi
ACTION="$1"; shift
case "$ACTION" in
  all)      MODULE='benchmarks.suite' ;;
  generate) MODULE='benchmarks.generate' ;;
  run)      MODULE='benchmarks.run' ;;
  evaluate) MODULE='benchmarks.evaluate' ;;
  test)     MODULE='pytest' ;;
  *) echo "Unknown benchmark command: $ACTION" >&2; exit 2 ;;
esac

# The API image includes the locked Python tooling, ReportLab, PDFium,
# and DejaVu fonts for reproducible Cyrillic benchmark PDF generation.
# Bind-mount ONLY benchmarks; do not change the running services.
# Run with the invoking host UID:GID so benchmark output is not root-owned.
# --build ensures first-time and updated Docker images include the fonts.
# env_file from Compose supplies OPENROUTER_API_KEY only inside the local container.
if [[ "$ACTION" == 'test' ]]; then
  docker compose run --build --rm --no-deps -T \
    --user "$(id -u):$(id -g)" --env HOME=/tmp \
    --volume "$ROOT/benchmarks:/app/benchmarks" --workdir /app api \
    /app/backend/.venv/bin/python -m pytest -q -p no:cacheprovider benchmarks/tests "$@"
else
  docker compose run --build --rm --no-deps -T \
    --user "$(id -u):$(id -g)" --env HOME=/tmp \
    --volume "$ROOT/benchmarks:/app/benchmarks" --workdir /app api \
    /app/backend/.venv/bin/python -m "$MODULE" "$@"
fi
