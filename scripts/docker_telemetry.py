#!/usr/bin/env python3
"""Read-only host Docker CLI collector for this Compose project; stdlib only.

Use `scripts/telemetry.sh start|stop|status|once`. Never run with sudo.
No Docker socket is mounted in containers. Only aggregate numeric data is sent
through the authenticated host-loopback API port.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib import request

ROOT = Path(__file__).resolve().parent.parent
SERVICES = {"api", "frontend", "worker", "redis", "rustfs", "opensearch"}
MEMORY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?i?B|B)$", re.I)
UNITS = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
         "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def cli(*args: str, timeout: int = 10) -> str:
    return subprocess.run(["docker", *args], cwd=ROOT, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=timeout).stdout


def environment() -> dict[str, str]:
    """Read only the two collector settings; never source/evaluate .env shell."""
    config = {}
    file = ROOT / ".env"
    if file.exists():
        for line in file.read_text(encoding="utf-8").splitlines():
            if line.startswith(("DOCKER_TELEMETRY_TOKEN=", "DOCKER_TELEMETRY_ENABLED=")):
                key, value = line.split("=", 1)
                config[key] = value.strip().strip('"').strip("'")
    return config


def byte_count(text: str) -> int | None:
    match = MEMORY.match(text.strip())
    if not match:
        return None
    multiplier = UNITS.get(match.group(2).lower())
    return int(float(match.group(1)) * multiplier) if multiplier is not None else None


def cpu_count(cpuset: str) -> int | None:
    if not cpuset:
        return None
    cpus = set()
    try:
        for chunk in cpuset.split(','):
            a, dash, b = chunk.partition('-')
            first = int(a)
            last = int(b) if dash else first
            if first < 0 or last < first or last - first > 2048:
                return None
            cpus.update(range(first, last + 1))
        return len(cpus) or None
    except ValueError:
        return None


def numeric_percent(value: str) -> float | None:
    try:
        result = float(value.removesuffix('%'))
        return result if 0 <= result <= 64000 else None
    except (ValueError, TypeError, AttributeError):
        return None


def processes_and_threads(container_id: str) -> tuple[int | None, int | None]:
    try:
        rows = cli('top', container_id, '-eo', 'pid,nlwp', timeout=3).splitlines()[1:]
        counts = [int(row.split()[1]) for row in rows if len(row.split()) >= 2]
        return len(counts), sum(counts)
    except (subprocess.SubprocessError, ValueError, IndexError):
        return None, None


def inspect_limits(container_ids: list[str]) -> dict[str, dict]:
    # A narrow Go template keeps env, command lines, host paths and socket
    # information out of the command's output even on the collector host.
    template = ('{{.Id}}|{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|'
                '{{.HostConfig.CpuQuota}}|{{.HostConfig.CpuPeriod}}|'
                '{{.HostConfig.CpusetCpus}}')
    output = cli('inspect', '--format', template, *container_ids)
    limits = {}
    for line in output.splitlines():
        parts = line.split('|')
        if len(parts) != 6:
            continue
        ident, mem, nano, quota, period, cpuset = parts
        try:
            memory_limit = int(mem)
            cpus = int(nano)
            cpu_quota = int(quota)
            cpu_period = int(period)
        except ValueError:
            continue
        allowed = cpus / 1e9 if cpus > 0 else (cpu_quota / cpu_period if cpu_quota > 0 and cpu_period > 0 else None)
        limits[ident[:12]] = {
            'memory_limit_bytes': memory_limit if memory_limit > 0 else None,
            'cpu_quota': round(allowed, 4) if allowed is not None else None,
            'cpuset_cpus': cpu_count(cpuset),
        }
    return limits


def sample() -> list[dict]:
    """Discover only the current Compose project's allowed, running services."""
    listing = cli('compose', 'ps', '--all', '--format', 'json', timeout=10).strip()
    if not listing:
        return []
    # Compose versions may return one JSON array or one JSON object per line.
    if listing.startswith('['):
        containers = json.loads(listing)
    else:
        containers = [json.loads(line) for line in listing.splitlines()]
    targets = []
    for item in containers:
        service = item.get('Service')
        ident = item.get('ID', '')
        if service not in SERVICES or not re.fullmatch(r'[a-f0-9]{12,64}', ident):
            continue
        name = item.get('Name', '')
        match = re.search(r'-(\d+)$', name)
        replica = int(match.group(1)) if match else 1
        if replica < 1 or replica > 32 or (service != 'worker' and replica != 1):
            continue
        state = item.get('State', 'unknown')
        if state not in ('running', 'exited', 'created', 'paused', 'restarting', 'dead'):
            state = 'unknown'
        targets.append({'id': ident[:12], 'full_id': ident, 'service': service, 'replica': replica,
                        'state': state, 'health': item.get('Health') or 'none'})
    targets = sorted(targets, key=lambda d: (d['service'], d['replica']))[:16]
    if not targets:
        return []
    ids = [target['full_id'] for target in targets]
    limits = inspect_limits(ids)
    running = [target['full_id'] for target in targets if target['state'] == 'running']
    stats_lines = cli('stats', '--no-stream', '--format', '{{json .}}', *running, timeout=20) if running else ''
    stats = {}
    for line in stats_lines.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        ident = str(entry.get('ID', ''))[:12]
        if ident:
            stats[ident] = entry
    result = []
    for target in targets:
        ident = target['id']
        info = stats.get(ident, {})
        processes, threads = processes_and_threads(target['full_id']) if target['state'] == 'running' else (None, None)
        used = byte_count(str(info.get('MemUsage', '')).partition('/')[0])
        item = {k: v for k, v in target.items() if k != 'full_id'}
        item.update(limits.get(ident, {'memory_limit_bytes': None, 'cpu_quota': None, 'cpuset_cpus': None}))
        item['memory_used_bytes'] = used
        item['cpu_percent'] = numeric_percent(info.get('CPUPerc', ''))
        item['processes'] = processes
        item['threads'] = threads
        if item['health'] not in ('healthy', 'unhealthy', 'starting', 'none'):
            item['health'] = 'unknown'
        result.append(item)
    return result


def publish(samples: list[dict], token: str) -> None:
    payload = json.dumps({'containers': samples}, separators=(',', ':')).encode()
    req = request.Request('http://127.0.0.1:8000/api/v1/system/docker-telemetry',
                          data=payload, method='POST',
                          headers={'Content-Type': 'application/json', 'X-Docker-Telemetry-Token': token})
    with request.urlopen(req, timeout=5) as response:
        if response.status != 204:
            raise RuntimeError('Docker telemetry ingest unsuccessful')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Sample and publish once')
    parser.add_argument('--interval', type=int, default=5)
    args = parser.parse_args()
    if os.geteuid() == 0:
        print('Refusing root: run Docker telemetry as your normal user.', file=sys.stderr)
        return 2
    if not 3 <= args.interval <= 60:
        parser.error('--interval must be 3..60 seconds')
    config = environment()
    token = config.get('DOCKER_TELEMETRY_TOKEN', '')
    if config.get('DOCKER_TELEMETRY_ENABLED', '').lower() != 'true' or not re.fullmatch(r'[a-f0-9]{64}', token):
        print('Docker telemetry is not configured. Run ./scripts/setup.sh.', file=sys.stderr)
        return 2
    missing = 0
    while True:
        try:
            readings = sample()
            if readings:
                missing = 0
                publish(readings, token)
            else:
                missing += 1
                if missing >= 12:  # auto-exit after stack is removed
                    return 0
        except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
            # Do not print Docker command output, environment, or credentials.
            print(f'Docker telemetry sample unavailable: {type(exc).__name__}', file=sys.stderr, flush=True)
        if args.once:
            return 0 if missing == 0 else 1
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
