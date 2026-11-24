#!/usr/bin/env bash
# One command to execute the full showcase gate inside Docker.
# Default includes paid OpenRouter tests; --offline runs no paid calls.
set -euo pipefail
cd "$(dirname "$0")/.."
RELEASE_VERSION="$(awk -F '"' '/^version = / {print $2; exit}' backend/pyproject.toml)"
MODE=full
case "${1:-}" in
  '') ;;
  --offline) MODE=offline ;;
  --quick) MODE=quick ;;
  *) echo 'Usage: ./scripts/test_release.sh [--offline|--quick]' >&2; exit 2 ;;
esac
if [[ "$#" -gt 1 ]]; then echo 'Too many arguments.' >&2; exit 2; fi
mkdir -p test-results
stamp="$(date +%Y%m%d-%H%M%S)"
logfile="test-results/v${RELEASE_VERSION}-${stamp}.log"
printf 'PDF Processor v%s verification | mode: %s\n' "$RELEASE_VERSION" "$MODE"
printf 'Full log: %s\n' "$logfile"
if [[ "$MODE" == full ]]; then
  printf 'Includes PAID OpenRouter calls across PyPDF, native-PDF AI and image OCR.\n'
  ./scripts/verify.sh --live --full 2>&1 | tee "$logfile"
elif [[ "$MODE" == quick ]]; then
  printf 'Includes PAID OpenRouter probes and one PyPDF end-to-end workflow.\n'
  ./scripts/verify.sh --live --quick 2>&1 | tee "$logfile"
else
  ./scripts/verify.sh 2>&1 | tee "$logfile"
fi
printf '\nSUCCESS: release checks passed. Log: %s\n' "$logfile"
