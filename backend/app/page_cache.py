"""Best-effort, content-addressed cross-document cache for AI-extracted pages.

A failed extraction is never cached. A successful blank-page sentinel is
normalized to an empty Markdown string, which is a valid Redis cache hit.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.cache import page_fingerprint, page_key
from app.config import Settings

log = logging.getLogger(__name__)


class PageCache:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    async def get_or_extract(self, content: bytes, *, parser: str, settings: Settings,
                             page: int, extract: Callable[[], Awaitable[str]]) -> str:
        digest = page_fingerprint(content, parser, settings)
        key = page_key(digest)
        model = settings.native_pdf_model if parser == "gemini" else settings.image_ocr_model
        try:
            cached = await self.redis.get(key)
        except (RedisError, OSError):
            # Page-cache outages must not prevent the existing extraction path.
            log.warning("Page cache lookup unavailable page=%d", page,
                        extra={"event": "page.cache.unavailable", "page": page, "model": model, "parser": parser})
        else:
            if cached is not None:
                log.info("Page cache hit page=%d", page,
                         extra={"event": "page.cache.hit", "page": page, "model": model, "parser": parser})
                return cached.decode("utf-8") if isinstance(cached, bytes) else cached
            log.info("Page cache miss page=%d", page,
                     extra={"event": "page.cache.miss", "page": page, "model": model, "parser": parser})

        # Complete extraction before writing; failures never poison the cache.
        result = await extract()
        was_blank = result.strip() == "__BLANK_PAGE__"
        markdown = "" if was_blank else result
        if not markdown.strip() and not was_blank:
            # Never store an invalid empty provider response.
            return markdown
        try:
            await self.redis.set(key, markdown, ex=self.ttl_seconds)
        except (RedisError, OSError):
            log.warning("Page cache write unavailable page=%d", page,
                        extra={"event": "page.cache.unavailable", "page": page, "model": model, "parser": parser})
        else:
            log.info("Page cached page=%d", page,
                     extra={"event": "page.cache.stored", "page": page, "model": model, "parser": parser})
        return markdown
