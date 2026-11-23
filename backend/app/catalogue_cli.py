"""Initialize or replay catalogue metadata. Never deletes indexes or objects."""
import argparse
import asyncio
import json
from app.archive_store import ArchiveStore
from app.config import get_settings
from app.document_repository import DocumentRepository
from app.index_manager import IndexManager
from app.opensearch_client import OpenSearchClient
from app.reconciliation import rebuild
from app.run_repository import RunRepository


async def main(action):
    if action == "rebuild":
        from app.maintenance_cli import main as maintain
        return await maintain()
    settings = get_settings()
    store = ArchiveStore(settings)
    client = OpenSearchClient(settings.opensearch_url)
    try:
        indexes = IndexManager(client)
        if action == 'init':
            await indexes.ensure()
            print('PASS catalogue indexes and aliases have schema v1')
        else:
            await store.ready()
            documents = DocumentRepository(store, indexes)
            print(json.dumps(await rebuild(documents, RunRepository(store, indexes, documents))))
    finally:
        await client.aclose()
        store.client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'rebuild'))
    asyncio.run(main(parser.parse_args().action))
