"""Non-destructive metadata replay. Full deletion/online reconciliation is Stage 7."""
from uuid import UUID
from app.document_repository import DocumentRepository
from app.run_repository import RunRepository


async def rebuild(documents: DocumentRepository, runs: RunRepository, *, document_id=None):
    """Recreate missing indexes; replay versioned S3 manifests, never infer/delete.

    Run while no archive mutation is in progress for a complete snapshot.
    Failed/partial/active manifests are not rewritten; no AI or Redis access.
    """
    await documents.indexes.ensure()
    counts = {'documents': 0, 'runs': 0}
    # Two passes; don't depend on S3 ordering, and avoid holding the archive in RAM.
    for kind in ('documents', 'runs'):
        async for key in documents.archive.iter_manifest_keys(document_id):
            parts = key.split('/')
            if kind == 'documents' and len(parts) == 2:
                await documents.indexes.publish(await documents.get(parts[0]))
                counts[kind] += 1
            elif kind == 'runs' and len(parts) == 4:
                await documents.get(parts[0])  # fail visibly on orphan manifest
                await runs.indexes.publish(await runs.get(parts[0], UUID(parts[2])))
                counts[kind] += 1
    return counts
