"""Isolated S3 restart persistence probe. Never contacts OpenRouter or Redis."""
import asyncio
import sys
from pathlib import Path
from uuid import UUID, uuid4

from app.config import get_settings
from app.object_store import S3ObjectStore


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: phase3_storage_probe {create|verify|cleanup} [uuid]")
    store = S3ObjectStore(get_settings())
    action = sys.argv[1]
    if action == "create":
        uid = uuid4()
        await store.put(uid, Path("/app/samples/basic_text.pdf"))
        print(uid)
    else:
        uid = UUID(sys.argv[2])
        if action == "verify":
            target = Path(f"/tmp/phase3-probe-{uid}.pdf")
            try:
                await store.fetch(uid, target)
                if target.read_bytes() != Path("/app/samples/basic_text.pdf").read_bytes():
                    raise SystemExit("S3 object was changed after restart")
            finally:
                target.unlink(missing_ok=True)
        elif action == "cleanup":
            await store.remove(uid)
        elif action == "exists":
            if not await store.exists(uid):
                raise SystemExit("S3 object is missing")
        else:
            raise SystemExit("Unknown action")


if __name__ == "__main__":
    asyncio.run(main())
