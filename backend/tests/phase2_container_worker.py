"""Isolated Redis/worker crash demo harness; never contacts OpenRouter.

Called only by scripts/demo_container_failover.sh inside disposable containers.
Commands: create, work, cleanup DOCUMENT_ID.
"""
import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from redis.asyncio import Redis

from app.config import get_settings
from app.document_processor import DocumentProcessor
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.storage import StagedFile
from app.object_store import S3ObjectStore
from app.worker import DocumentWorker


class DemoSummarizer:
    def __init__(self, client, marker, mode):
        self.client, self.marker, self.mode = client, marker, mode

    async def summarize(self, pages):
        if self.mode == "stall":
            await self.client.set(self.marker, "1", ex=300)
            await asyncio.Event().wait()  # docker kill ends this container
        return "Recovered by the second Docker worker (mock summarizer)."


async def main():
    prefix = os.environ.get("DEMO_PREFIX", "")
    if not prefix.startswith("phase2demo:") or len(prefix) > 100:
        raise SystemExit("DEMO_PREFIX must start with phase2demo:")
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=5)
    stream = f"{prefix}:jobs"
    group = f"{prefix}:group"
    repo = DocumentRepository(redis, f"{prefix}:doc", result_ttl=600)
    mode = os.environ.get("DEMO_MODE", "survive")
    queue = DocumentQueue(redis, stream, group, f"demo-{mode}-{uuid4().hex[:12]}")
    store = S3ObjectStore(settings)
    try:
        if sys.argv[1] == "create":
            await queue.ensure_group()
            doc_id = uuid4()
            source = Path("/app/samples/basic_text.pdf")
            await store.put(doc_id, source)
            doc = StagedFile(doc_id, "demo.pdf", ParserChoice.pypdf,
                             source, source, source.stat().st_size)
            await queue.enqueue_batch([doc], repo)
            print(doc_id, flush=True)
        elif sys.argv[1] == "work":
            processor = DocumentProcessor(settings, repo, summarizer=DemoSummarizer(
                redis, f"{prefix}:started", mode))
            worker = DocumentWorker(settings, queue, repo, processor, object_store=store)
            await worker.run_forever()
        elif sys.argv[1] == "cleanup":
            doc_id = UUID(sys.argv[2])
            await redis.delete(stream, f"{prefix}:started", *(
                repo.key(doc_id, suffix) for suffix in ("meta", "pages", "summary")))
            await store.remove(doc_id)
        elif sys.argv[1] == "exists":
            doc_id = UUID(sys.argv[2])
            if await store.exists(doc_id):
                raise SystemExit(1)
        else:
            raise SystemExit("Usage: ... phase2_container_worker {create|work|cleanup|exists} [id]")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
