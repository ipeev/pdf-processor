"""Contract tests for the uv tooling (no network or Docker daemon required)."""

from pathlib import Path
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def test_python_version_pin_matches_project_requirement():
    assert (BACKEND / ".python-version").read_text().strip() == "3.15.2"
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    assert project["project"]["requires-python"] == ">=3.15,<3.16"
    assert project["project"]["name"] == "async-pdf-processor"


def test_docker_uses_uv_for_dependency_installation():
    dockerfile = (BACKEND / "Dockerfile").read_text()
    assert "python:3.15.2-slim-trixie" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.13.8" in dockerfile
    assert "uv sync --locked --extra test" in dockerfile
    assert "pip install" not in dockerfile
    assert "UV_PYTHON_DOWNLOADS=never" in dockerfile
    # uv must run with pyproject.toml in the current directory; tests also
    # expect repository-relative paths to match the host checkout.
    assert "WORKDIR /app/backend" in dockerfile
    assert 'PATH="/app/backend/.venv/bin:$PATH"' in dockerfile


def test_lockfile_bootstrap_and_verification_are_available():
    bootstrap = (ROOT / "scripts" / "lock_dependencies.sh").read_text()
    verify = (ROOT / "scripts" / "verify.sh").read_text()
    assert "api lock --python /usr/local/bin/python3.15" in bootstrap
    setup = (ROOT / "scripts" / "setup.sh").read_text()
    assert "backend_lock_requires_python" in setup
    assert "scripts/lock_dependencies.sh and reverify" in setup
    assert "scripts/lock_frontend.sh and reverify" in setup
    assert "bundled Python and npm dependency lock versions match" in setup
    assert '"$backend_lock_requires_python" != "$backend_requires_python"' in setup
    assert "uv lock --check" in verify
    assert "sys.version_info[:3] == (3, 15, 2)" in verify
    assert "uv run --locked" in verify


@pytest.mark.parametrize("script_name", ["demo.sh", "verify.sh", "lock_dependencies.sh", "setup.sh", "check_workflow.sh"])
def test_scripts_have_valid_bash_syntax(script_name):
    script = ROOT / "scripts" / script_name
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_primary_container_scripts_do_not_depend_on_host_python():
    # demo.sh can intentionally use host Python 3 for arbitrary host paths.
    # Setup, verification and bundled sample workflows stay inside Docker/uv.
    for script_name in ["setup.sh", "check_workflow.sh", "verify.sh", "lock_dependencies.sh"]:
        script = (ROOT / "scripts" / script_name).read_text()
        assert "python3 -" not in script
        assert "python3 -m" not in script
