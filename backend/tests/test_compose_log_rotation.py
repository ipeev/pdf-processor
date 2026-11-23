"""v0.11.1 log-rotation contract; runs without Docker or a YAML package."""
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
SERVICES = {"frontend", "redis", "rustfs-permissions", "rustfs", "api", "worker", "opensearch"}


def test_json_file_rotation_is_bounded_and_shared_by_all_services():
    source = COMPOSE.read_text()
    assert re.search(r'(?m)^x-log-rotation: &log-rotation\s*$', source)
    policy = source.split('x-log-rotation: &log-rotation\n', 1)[1].split('\nservices:', 1)[0]
    assert re.search(r'(?m)^  driver: json-file\s*$', policy)
    assert re.search(r'(?m)^  options:\s*$', policy)
    assert re.search(r'(?m)^    max-size: "20m"\s*$', policy)
    assert re.search(r'(?m)^    max-file: "5"\s*$', policy)

    body = source.split('\nservices:\n', 1)[1].split('\nvolumes:', 1)[0]
    blocks = re.findall(r'(?ms)^  ([a-z][a-z0-9-]*):\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)', body)
    assert {name for name, _ in blocks} == SERVICES
    for name, block in blocks:
        assert re.search(r'(?m)^    logging: \*log-rotation\s*$', block), name
        assert block.count('logging:') == 1, name


def test_json_application_logs_remain_the_default():
    # The backend Docker image excludes .env.example (never a runtime secret).
    # Validate the shipped default in the always-present Python config.
    from app.config import Settings
    assert Settings(_env_file=None, openrouter_api_key="").log_format == "json"
    example = ROOT / '.env.example'
    if example.exists():
        assert re.search(r'(?m)^LOG_FORMAT=json$', example.read_text())


def test_live_verification_script_is_bash_syntax_valid():
    script = ROOT / 'scripts/check_log_rotation.sh'
    assert script.is_file()
    subprocess.run(['bash', '-n', str(script)], check=True)
    assert 'bash scripts/check_log_rotation.sh' in (ROOT / 'scripts/verify.sh').read_text()


def test_generic_otel_host_gateway_without_compose_override():
    from pathlib import Path
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()
    assert compose.count('host.docker.internal:host-gateway') == 2
    assert 'observability_ingest' not in compose
