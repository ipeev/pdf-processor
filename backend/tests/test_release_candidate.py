"""Stage 9 offline release acceptance: exact locked versions and isolated restart.

No Docker daemon, remote provider or external dependency resolution required.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_lock_provenance_and_exact_versions():
    import sys
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_release_candidate.py')],
                            check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'no dependency resolution' in result.stdout


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
