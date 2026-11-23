#!/usr/bin/env bash
# Control the small non-root host collector. No sudo, systemd, or extra container.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(id -u)" -eq 0 ]]; then echo 'Run as your normal user, never root.' >&2; exit 2; fi
project_id="$(printf '%s' "$PWD" | sha256sum | cut -c1-12)"
state_dir="${XDG_RUNTIME_DIR:-$HOME/.cache}/pdf-processor-$project_id"
mkdir -p "$state_dir"
chmod 700 "$state_dir"
pidfile="$state_dir/telemetry.pid"
logfile="$state_dir/telemetry.log"
identity="$(realpath scripts/docker_telemetry.py)"
active() {
  [[ -s "$pidfile" ]] || return 1
  local pid
  pid="$(cat "$pidfile")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # Avoid signaling an unrelated process after PID reuse.
  [[ "$(ps -p "$pid" -o args= 2>/dev/null)" == *"$identity"* ]]
}
case "${1:-}" in
  start)
    if active; then echo "Docker telemetry already running (PID $(cat "$pidfile"))."; exit 0; fi
    rm -f "$pidfile"
    ( umask 077; PYTHONDONTWRITEBYTECODE=1 nohup python3 "$identity" --interval 5 >>"$logfile" 2>&1 </dev/null & echo "$!" >"$pidfile" )
    sleep 0.2
    if active; then echo "Docker telemetry running; stop with ./scripts/telemetry.sh stop"; else
      echo "Docker telemetry failed to start; see $logfile" >&2; exit 1
    fi
    ;;
  stop)
    if active; then kill "$(cat "$pidfile")"; echo 'Docker telemetry stopped.'; else echo 'Docker telemetry not running.'; fi
    rm -f "$pidfile"
    ;;
  status)
    if active; then echo "Docker telemetry running (PID $(cat "$pidfile"))."; else echo 'Docker telemetry stopped.'; fi
    ;;
  once)
    PYTHONDONTWRITEBYTECODE=1 python3 "$identity" --once
    ;;
  *) echo 'Usage: ./scripts/telemetry.sh {start|stop|status|once}' >&2; exit 2 ;;
esac
