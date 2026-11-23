"""Static security contracts for container users and writable host bind mounts."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_backend_image_runs_as_unprivileged_user():
    source = (ROOT / "backend/Dockerfile").read_text()
    assert "USER 10001:10001" in source
    assert "HOME=/tmp" in source
    assert "UV_CACHE_DIR=/tmp/uv-cache" in source
    # Never populate the runtime cache as root during image construction.
    assert "--mount=type=cache,target=/root/.cache/uv,sharing=locked" in source
    assert "export UV_CACHE_DIR=/root/.cache/uv;" in source


def test_app_services_do_not_run_as_root_or_mount_host_directories():
    source = (ROOT / "docker-compose.yml").read_text()
    for service, ending in (("api", "  worker:"), ("worker", "\nvolumes:")):
        section = source.split("\n  " + service + ":\n", 1)[1].split(ending, 1)[0]
        assert 'user: "10001:10001"' in section
        assert 'cap_drop: ["ALL"]' in section
        assert 'security_opt: ["no-new-privileges:true"]' in section
        import yaml
        compose = yaml.safe_load(source)
        assert compose["services"][service]["volumes"] == ["archive_coordination:/coordination"]
        assert compose["services"][service]["environment"]["ARCHIVE_COORDINATION_FILE"] == "/coordination/archive.lock"


def test_named_volume_initializer_is_limited_and_not_a_host_mount():
    source = (ROOT / "docker-compose.yml").read_text()
    section = source.split("  rustfs-permissions:", 1)[1].split("  rustfs:", 1)[0]
    assert 'network_mode: "none"' in section
    assert 'cap_drop: ["ALL"]' in section
    assert 'cap_add: ["CHOWN"]' in section
    assert 'rustfs_data:/data' in section
    assert 'chown 10001:10001 /data' in section
    assert 'chown -R' not in section
    assert '/home/' not in section


def test_benchmark_and_lockfile_tools_reject_root_host_execution():
    for name in ("benchmark.sh", "lock_dependencies.sh", "lock_frontend.sh"):
        source = (ROOT / "scripts" / name).read_text()
        assert '"$(id -u)" -eq 0' in source
        assert '--user "$(id -u):$(id -g)"' in source


def test_frontend_verification_host_bind_is_read_only():
    source = (ROOT / "scripts/verify.sh").read_text()
    assert 'frontend:/app/frontend:ro' in source
    assert 'runtime_uid' in source
    # A secure dev install uses umask 077, so the test files must be staged
    # for UID 10001 rather than granting access to the actual checkout.
    assert 'test_frontend="$(mktemp -d ' in source
    assert 'chmod -R a+rX -- "$test_frontend"' in source
    assert 'trap cleanup_test_frontend EXIT' in source
    assert 'frontend/.env' not in source
    assert 'user: "0:0"' not in source
