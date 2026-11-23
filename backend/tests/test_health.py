import httpx
import pytest
from app.main import app
from app.object_store import LocalTestObjectStore
from app.redis_store import DocumentRepository
from app.queue import DocumentQueue
from tests.fake_redis import FakeRedis


@pytest.mark.asyncio
async def test_health_and_ready(settings):
    fake = FakeRedis()
    app.state.redis = fake
    app.state.settings = settings
    app.state.object_store = LocalTestObjectStore(settings.upload_dir)
    app.state.repository = DocumentRepository(fake)
    app.state.queue = DocumentQueue(fake, "health:queue", "health:group", "worker-1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/health")).status_code == 200
        assert (await client.get("/api/v1/ready")).json()["status"] == "ready"

@pytest.mark.asyncio
async def test_ready_returns_503_when_redis_down(settings):
    fake = FakeRedis()
    fake.available = False
    app.state.redis = fake
    app.state.settings = settings
    app.state.object_store = LocalTestObjectStore(settings.upload_dir)
    app.state.repository = DocumentRepository(fake)
    app.state.queue = DocumentQueue(fake, "health:queue", "health:group", "worker-1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/health")).status_code == 200
        unavailable = await client.get("/api/v1/ready")
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "REDIS_UNAVAILABLE"
