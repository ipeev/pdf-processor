"""Stage 9 offline release acceptance: exact locked versions and isolated restart.

No Docker daemon, remote provider or external dependency resolution required.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_lock_provenance_and_exact_versions():
    import sys
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_release_candidate.py'), '--runtime-only'],
                            check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'no dependency resolution' in result.stdout


def test_release_checker_distinguishes_source_checkout_from_minimal_image(tmp_path):
    """The runtime image has locked metadata but never public docs or .github."""
    import shutil
    import sys
    for rel in ('scripts/check_release_candidate.py', 'backend/pyproject.toml',
                'backend/uv.lock', 'frontend/package.json',
                'frontend/package-lock.json', 'docker-compose.yml'):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    tool = str(tmp_path / 'scripts/check_release_candidate.py')
    runtime = subprocess.run([sys.executable, tool, '--runtime-only'],
                             capture_output=True, text=True)
    assert runtime.returncode == 0, runtime.stdout + runtime.stderr
    assert 'runtime metadata' in runtime.stdout
    # Full-source validation must not silently bypass missing documentation.
    source = subprocess.run([sys.executable, tool], capture_output=True, text=True)
    assert source.returncode != 0
    assert 'missing public documentation: README.md' in source.stderr


def test_compose_release_labels_change_on_upgrade():
    """Application containers must be recreated when a new release is deployed."""
    import re
    from pathlib import Path
    import tomllib
    version = tomllib.loads((ROOT / 'backend/pyproject.toml').read_text())['project']['version']
    compose = (ROOT / 'docker-compose.yml').read_text()
    for name in ('frontend', 'api', 'worker'):
        match = re.search(
            rf'(?ms)^  {name}:\n    labels:\n      org\.opencontainers\.image\.version: "([^"]+)"',
            compose,
        )
        assert match and match.group(1) == version, (name, match.group(1) if match else None)


def test_setup_rejects_running_container_version_drift():
    setup = (ROOT / 'scripts/setup.sh').read_text()
    assert 'for service in api worker frontend; do' in setup
    assert 'while IFS= read -r container_id; do' in setup
    assert 'docker inspect --format' in setup
    assert 'docker exec "$container_id" python -c' in setup
    assert 'running_version' in setup
    assert 'running version=$running_version' in setup
    assert '--force-recreate api worker frontend' in setup
    assert 'down -v' not in setup.split('for service in api worker frontend; do')[1].split('=== Containers ===')[0]


def test_runtime_metadata_mismatch_reports_observed_version(tmp_path):
    """A mixed release reports what the image contains, not a cryptic assertion."""
    import shutil
    import sys
    for rel in ('scripts/check_release_candidate.py', 'backend/pyproject.toml',
                'backend/uv.lock', 'frontend/package.json',
                'frontend/package-lock.json', 'docker-compose.yml'):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    project = tmp_path / 'backend/pyproject.toml'
    project.write_text(project.read_text().replace('version = "0.12.8"', 'version = "0.12.7"'))
    check = subprocess.run([sys.executable, str(tmp_path / 'scripts/check_release_candidate.py'), '--runtime-only'],
                           capture_output=True, text=True)
    assert check.returncode != 0
    assert "found version='0.12.7'" in check.stderr
    assert 'running Docker image' in check.stderr


def test_verify_checks_checkout_before_image_and_image_uses_runtime_mode():
    script = (ROOT / 'scripts/verify.sh').read_text()
    source_check = 'python3 scripts/check_release_candidate.py'
    startup = './scripts/setup.sh --no-prompt'
    runtime_check = 'python /app/scripts/check_release_candidate.py --runtime-only'
    assert script.index(source_check) < script.index(startup)
    assert runtime_check in script


def test_container_install_fails_closed_on_missing_locks():
    docker = (ROOT / 'backend/Dockerfile').read_text()
    assert 'test -s uv.lock' in docker
    assert 'COPY frontend/package.json frontend/package-lock.json ./frontend/' in docker
    assert 'uv sync --locked --extra test' in docker
    assert 'uv sync --extra test' not in docker
    setup = (ROOT / 'scripts/setup.sh').read_text()
    assert 'will not re-resolve dependencies' in setup
    assert '==3.15.*' in setup
    assert './scripts/lock_dependencies.sh' not in setup
    assert './scripts/lock_frontend.sh' not in setup


def test_isolated_restart_must_use_dedicated_containers_bucket_indexes():
    shell = (ROOT / 'scripts/check_redis_restart_recovery.sh').read_text()
    python = (ROOT / 'backend/tests/rc_redis_restart.py').read_text()
    assert 'redis:7.4-alpine' in shell
    assert 'docker restart "$container"' in shell
    assert 'docker compose restart redis' not in shell
    assert '--no-deps -e "REDIS_URL=redis://$container:6379/0"' in shell
    assert '-e OPENROUTER_API_KEY=' in shell
    assert "'s3_archive_bucket': f'pdf-rc1-{TOKEN}'" in python
    assert "index_prefix = f\"pdf-test-{hashlib.sha256(TOKEN.encode('ascii')).hexdigest()[:32]}\"" in python
    assert 'indexes = IndexManager(search, prefix=index_prefix)' in python
    assert "result['recovered'] == 2" in python
    assert "repeated['recovered'] == 0" in python
    assert 'h.summary.calls == 0' in python


def test_isolated_restart_namespace_is_accepted_by_real_index_manager():
    """Catches a forbidden test prefix before the Docker restart gate runs."""
    import hashlib
    import pytest
    from app.index_manager import IndexManager

    token = '0123456789abcdef01234567'  # 24-hex disposable resource token
    prefix = f"pdf-test-{hashlib.sha256(token.encode('ascii')).hexdigest()[:32]}"
    assert IndexManager(None, prefix=prefix).prefix == prefix
    with pytest.raises(ValueError, match='Invalid catalogue namespace'):
        IndexManager(None, prefix=f'pdf-rc1-{token}')


def test_rc_gate_includes_all_prior_and_real_restart():
    verify = (ROOT / 'scripts/verify.sh').read_text()
    for required in ('test_phase2_failover.py', 'test_phase3_object_store.py',
                     'test_archive_store.py', 'test_catalogue.py', 'test_persistent_pipeline.py',
                     'test_library_api.py', 'test_archive_maintenance.py',
                     'test_archive_operations.py', 'check_storage_restart.sh',
                     'check_opensearch.sh --restart', 'check_frontend.sh',
                     'check_redis_restart_recovery.sh', 'check_release_candidate.py', 'check_release_surface.sh'):
        assert required in verify, required


def test_rc_shell_syntax_and_lifetime_scope():
    for relative in ('scripts/check_redis_restart_recovery.sh','scripts/check_release_surface.sh','scripts/setup.sh',
                     'scripts/verify.sh','scripts/test_release.sh'):
        script = ROOT / relative
        subprocess.run(['bash','-n',str(script)],check=True)
