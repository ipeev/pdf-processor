import json
from uuid import uuid4

import pytest

from app.errors import ProcessingError
from app.parsers.base import ParsedDocument, ParsedPage
from app.redis_store import DocumentRepository, verify_exec


@pytest.mark.asyncio
async def test_store_complete_and_read(redis_fake):
    repo = DocumentRepository(redis_fake)
    doc = uuid4()
    await redis_fake.hset(repo.key(doc, "meta"), mapping=repo.prepare_meta(doc, "demo.pdf", "pypdf"))
    await repo.mark_processing(doc)
    await repo.store_completed(doc, ParsedDocument([ParsedPage(1, "one"), ParsedPage(2, "")]), "A useful summary")
    meta = await repo.get_meta(doc)
    assert meta["status"] == "completed"
    assert meta["worker_attempts"] == "1"
    pages, summary = await repo.read_completed(doc)
    assert [p["page"] for p in pages] == [1, 2]
    assert summary == "A useful summary"


@pytest.mark.asyncio
async def test_failed_deletes_results(redis_fake):
    repo = DocumentRepository(redis_fake)
    doc = uuid4()
    await redis_fake.hset(repo.key(doc, "meta"), mapping=repo.prepare_meta(doc, "demo.pdf", "pypdf"))
    await redis_fake.set(repo.key(doc, "pages"), "garbage")
    await redis_fake.set(repo.key(doc, "summary"), "bad summary")
    await repo.store_failed(doc, ProcessingError("INVALID_PDF", "PDF unreadable"))
    assert (await repo.get_meta(doc))["status"] == "failed"
    assert await redis_fake.mget(repo.key(doc, "pages"), repo.key(doc, "summary")) == [None, None]


@pytest.mark.asyncio
async def test_incomplete_complete_fails_closed(redis_fake):
    repo = DocumentRepository(redis_fake)
    doc = uuid4()
    meta = repo.prepare_meta(doc, "demo.pdf", "pypdf")
    meta["status"] = "completed"
    meta["page_count"] = "2"
    await redis_fake.hset(repo.key(doc, "meta"), mapping=meta)
    await redis_fake.set(repo.key(doc, "pages"), json.dumps([{"page": 1, "markdown": "A"}]))
    await redis_fake.set(repo.key(doc, "summary"), "Summary")
    with pytest.raises(ProcessingError, match="RESULT_CORRUPT"):
        await repo.read_completed(doc)
    assert not await repo.is_complete(doc)


def test_exec_runtime_error_not_rollback():
    with pytest.raises(ProcessingError, match="REDIS_TRANSACTION_UNCERTAIN"):
        verify_exec([1, RuntimeError("wrong type"), 3], 3)
