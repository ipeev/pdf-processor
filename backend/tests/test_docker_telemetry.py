"""Resource-telemetry contract: trust boundary, bounded storage and host parsing."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import time

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.docker_telemetry import ContainerSample, DockerBatch, receive_samples, docker_snapshot
from app.api import router


SAMPLE = {"service": "worker", "replica": 1, "id": "123456789abc", "state": "running",
          "health": "healthy", "memory_used_bytes": 12000000, "memory_limit_bytes": None,
          "cpu_percent": 143.2, "cpu_quota": None, "cpuset_cpus": 4,
          "processes": 1, "threads": 29}


class TelemetryRedis:
    def __init__(self):
        self.kv = {}
        self.lists = {}
    async def set(self, key, value, ex=None):
        self.kv[key] = value
    async def get(self, key):
        return self.kv.get(key)
    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
    async def ltrim(self, key, start, stop):
        self.lists[key] = self.lists.get(key, [])[start:stop+1]
    async def expire(self, key, ttl):
        pass
    async def lrange(self, key, start, stop):
        return self.lists.get(key, [])[start:stop+1]


@pytest.mark.asyncio
async def test_store_and_retrieve_cpu_history_and_unknown_limits():
    redis = TelemetryRedis()
    assert (await docker_snapshot(redis))["status"] == "unavailable"
    sample = ContainerSample.model_validate(SAMPLE)
    await receive_samples(redis, DockerBatch(containers=[sample]))
    await receive_samples(redis, DockerBatch(containers=[sample.model_copy(update={"cpu_percent": 201.2})]))
    result = await docker_snapshot(redis)
    assert result["status"] == "online"
    assert result["containers"][0]["cpu_percent"] == 201.2
    assert [point["cpu_percent"] for point in result["containers"][0]["history"]] == [143.2, 201.2]
    assert result["containers"][0]["memory_limit_bytes"] is None
    redis.kv["monitor:docker:latest"] = json.dumps({"timestamp": time.time() - 25, "containers": [SAMPLE]})
    assert (await docker_snapshot(redis))["status"] == "unavailable"


@pytest.mark.asyncio
async def test_history_is_bounded_to_65_points():
    redis = TelemetryRedis()
    for index in range(85):
        await receive_samples(redis, DockerBatch(containers=[ContainerSample.model_validate(
            {**SAMPLE, "cpu_percent": float(index)})]))
    assert len((await docker_snapshot(redis))["containers"][0]["history"]) <= 65
    assert len(redis.lists["monitor:docker:history:123456789abc"]) == 65


@pytest.mark.asyncio
async def test_ingest_authentication_and_allowlist():
    redis = TelemetryRedis()
    # This endpoint test uses an isolated app instead of modifying the global
    # application State (which could leak into other tests during teardown).
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.redis = redis
    test_app.state.settings = Settings(_env_file=None, docker_telemetry_enabled=True,
                                       docker_telemetry_token="unit-test-unguessable-secret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://test") as http:
        route = '/api/v1/system/docker-telemetry'
        assert (await http.post(route, json={"containers": [SAMPLE]})).status_code == 403
        headers = {'X-Docker-Telemetry-Token': 'unit-test-unguessable-secret'}
        assert (await http.post(route, headers=headers, json={"containers": [SAMPLE]})).status_code == 204
        assert (await http.post(route, headers=headers, json={"containers": [{**SAMPLE, "service": "other"}]})).status_code == 422
        assert (await http.post(route, headers=headers, json={"containers": [{**SAMPLE, "docker_socket": "/var/run/docker.sock"}]})).status_code == 422
        assert (await http.post(route, headers=headers, content='x' * 40000)).status_code == 413
        test_app.state.settings = Settings(_env_file=None, docker_telemetry_enabled=False,
                                           docker_telemetry_token="unit-test-unguessable-secret")
        assert (await http.post(route, headers=headers, json={"containers": [SAMPLE]})).status_code == 403


def _load_collector():
    source = Path(__file__).resolve().parents[2] / "scripts" / "docker_telemetry.py"
    spec = importlib.util.spec_from_file_location('host_docker_telemetry', source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_host_sampler_scopes_project_and_does_not_emit_commands(monkeypatch):
    module = _load_collector()
    ids = ["a" * 64, "b" * 64]
    listing = [{"ID": ids[0], "Service": "worker", "Name": "demo-worker-1", "State": "running", "Health": "healthy"},
               {"ID": ids[1], "Service": "redis", "Name": "demo-redis-1", "State": "running", "Health": "healthy"},
               {"ID": "c" * 64, "Service": "foreign", "Name": "other", "State": "running", "Health": "healthy"}]
    def fake_cli(*args, **_kwargs):
        if args[:2] == ("compose", "ps"):
            return json.dumps(listing)
        if args[0] == "inspect":
            return f"{ids[0]}|0|0|0|100000|0-3\n{ids[1]}|1073741824|2000000000|0|100000|\n"
        if args[0] == "stats":
            return '\n'.join(json.dumps(item) for item in [
                {"ID": ids[0][:12], "MemUsage": "33MiB / 8GiB", "CPUPerc": "154.3%"},
                {"ID": ids[1][:12], "MemUsage": "48.3MiB / 1GiB", "CPUPerc": "2.5%"}])
        if args[0] == "top":
            return "PID NLWP\n100 21\n200 2\n" if args[1] == ids[0] else "PID NLWP\n100 5\n"
        raise AssertionError(args)
    monkeypatch.setattr(module, 'cli', fake_cli)
    result = module.sample()
    assert len(result) == 2
    assert result[1]['service'] == 'worker'
    assert result[1]['cpu_percent'] == 154.3
    assert result[1]['threads'] == 23 and result[1]['processes'] == 2
    assert result[1]['memory_limit_bytes'] is None
    assert result[0]['cpu_quota'] == 2.0
    assert result[1]['cpuset_cpus'] == 4
    assert 'Name' not in str(result) and 'HostConfig' not in str(result)
    DockerBatch.model_validate({'containers': result})


def test_parser_unknown_units_are_never_assumed_to_be_zero():
    module = _load_collector()
    assert module.byte_count('12MiB') == 12 * 1024 * 1024
    assert module.byte_count('N/A') is None
    assert module.cpu_count('0-2,4,8-9') == 6
    assert module.cpu_count('junk') is None


def test_opensearch_is_allowed_by_collector_and_ingest():
    assert 'opensearch' in _load_collector().SERVICES
    sample = ContainerSample.model_validate({**SAMPLE, 'service': 'opensearch'})
    assert sample.service == 'opensearch'
