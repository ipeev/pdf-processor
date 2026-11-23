import pytest
from pydantic import ValidationError
from app.config import Settings


def test_runtime_dependencies_import():
    import fastapi, pypdf, redis, httpx, pypdfium2
    assert fastapi and pypdf and redis and httpx and pypdfium2


def test_defaults_no_key_allowed():
    config = Settings(_env_file=None, openrouter_api_key="")
    assert not config.openrouter_api_key.get_secret_value()
    assert config.native_pdf_model == "inclusionai/ling-3.0-flash-vl"
    assert config.image_ocr_model == "deepseek/deepseek-v4.1-flash-1017"
    assert config.summary_model == "deepseek/deepseek-v4.1-flash-1017"
    assert config.worker_threads == 20
    assert config.max_pages_per_document == 200
    assert config.max_ai_pages_per_document == 200
    assert config.max_ai_output_tokens_per_page == 32768
    assert config.max_summary_output_tokens == 4096
    assert config.max_summary_input_chars == 400000


def test_secrets_masked():
    config = Settings(_env_file=None, openrouter_api_key="PRIVATE_TOKEN")
    assert "PRIVATE_TOKEN" not in repr(config)


@pytest.mark.parametrize("params", [
    {"max_file_size_mib": -1}, {"max_total_upload_mib": 1, "max_file_size_mib": 2},
    {"max_pages_per_document": 2, "max_ai_pages_per_document": 3},
    {"openrouter_max_call_attempts": 0}, {"worker_threads": 0}, {"worker_threads": 101},
    {"redis_url": "http://redis:6379/0"},
])
def test_bad_limits_rejected(params):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **params)


def test_custom_openrouter_token_limits_remain_configurable():
    config = Settings(
        _env_file=None,
        max_ai_output_tokens_per_page=8192,
        max_summary_output_tokens=1024,
        max_summary_input_chars=50000,
    )
    assert config.max_ai_output_tokens_per_page == 8192
    assert config.max_summary_output_tokens == 1024
    assert config.max_summary_input_chars == 50000


def test_fastapi_version_matches_python_package_metadata():
    from pathlib import Path
    import tomllib
    from app.main import app
    metadata = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert app.version == metadata["project"]["version"]
