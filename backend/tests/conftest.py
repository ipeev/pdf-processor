import os
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from app.config import Settings
from tests.fake_redis import FakeRedis

SAMPLES_DIR = next(p / "samples" for p in Path(__file__).resolve().parents if (p / "samples" / "basic_text.pdf").exists())


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        upload_dir=tmp_path,
        openrouter_api_key="",  # isolate offline tests from a real .env/API key
        redis_url="redis://localhost:6379/0",
        max_file_size_mib=1,
        max_total_upload_mib=3,
        max_pages_per_document=10,
        max_ai_pages_per_document=5,
        openrouter_max_call_attempts=2,
    )


@pytest.fixture
def redis_fake():
    return FakeRedis()


@pytest.fixture
def sample_basic():
    return SAMPLES_DIR / "basic_text.pdf"


@pytest.fixture
def sample_blank():
    return SAMPLES_DIR / "blank_middle_page.pdf"


@pytest.fixture
def sample_scanned():
    return SAMPLES_DIR / "scanned_image_only.pdf"


@pytest.fixture
def sample_table():
    return SAMPLES_DIR / "table_and_headings.pdf"


@pytest.fixture(autouse=True)
def skip_live_unless_opted_in(request):
    if request.node.get_closest_marker("live") and os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("paid OpenRouter test requires RUN_LIVE_TESTS=1")


@pytest.fixture
async def real_redis():
    """Actual Redis fixture; never silently substitutes the in-memory fake."""
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
    url = os.getenv("REDIS_TEST_URL", "redis://redis:6379/0")
    client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
    try:
        await client.ping()
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip("Redis unavailable; run real integration tests inside Docker Compose")
    try:
        yield client
    finally:
        await client.aclose()
