"""Single-host, cross-process archive read/write coordination.

All live admission/mutation and full worker jobs hold shared locks. Maintenance
holds an exclusive lock, never blocks a processing worker indefinitely, and
checks Redis stream emptiness separately. A process exit releases its flock.
The lock inode must never be unlinked while services run.
"""
import asyncio
import fcntl
import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from app.errors import ProcessingError


@asynccontextmanager
async def archive_gate(*, exclusive=False, path=None, wait=True):
    path = Path(path or os.environ.get('ARCHIVE_COORDINATION_FILE', '/tmp/pdf-archive-coordination.lock'))
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise ProcessingError('ARCHIVE_COORDINATION_UNAVAILABLE', 'Archive coordination is unavailable.', 503) from exc
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if exclusive or not wait:
                    raise ProcessingError('ARCHIVE_BUSY', 'Archive maintenance requires idle processing. Retry when jobs finish.', 409)
                await asyncio.sleep(.05)
        yield
    finally:
        os.close(fd)


async def finish_before_unlock(awaitable):
    """Client disconnect/cancellation must not release a gate around live I/O."""
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


async def guarded(pipeline, operation, *, reconcile_on_error=False):
    async with archive_gate():
        await pipeline.archive.assert_no_deletion()
        try:
            return await finish_before_unlock(operation())
        except Exception as exc:
            if reconcile_on_error and (not isinstance(exc, ProcessingError) or exc.http_status >= 500):
                try:
                    await pipeline.archive.mark_reconciliation_needed()
                except ProcessingError:
                    logging.getLogger(__name__).warning('Could not persist archive repair marker')
            raise
