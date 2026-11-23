"""v0.10.0 resource policy; no Docker daemon or third-party YAML parser needed."""
import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _service_body(name: str) -> str:
    yaml = COMPOSE.read_text()
    sections = re.split(r"(?=^  [a-z][a-z0-9-]*:\s*$)", yaml, flags=re.MULTILINE)
    return next(block for block in sections if block.startswith(f"  {name}:\n"))


def test_worker_replicas_have_four_gib_each():
    worker = _service_body("worker")
    assert re.search(r"^    scale: 2$", worker, re.MULTILINE)
    assert re.search(r"^    mem_limit: 4g$", worker, re.MULTILINE)


def test_other_long_running_containers_have_two_gib_caps():
    for name in ("api", "frontend", "redis", "rustfs", "opensearch"):
        assert re.search(r"^    mem_limit: 2g$", _service_body(name), re.MULTILINE), name


def test_cpu_policy_unchanged_and_initializer_not_constrained():
    yaml = COMPOSE.read_text()
    assert not re.search(r"^    (cpus|cpu_quota|cpuset):", yaml, re.MULTILINE)
    assert "mem_limit:" not in _service_body("rustfs-permissions")
