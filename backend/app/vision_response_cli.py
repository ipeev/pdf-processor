"""Print an archived vision model reply verbatim (local diagnostic; no AI calls).

Usage: python -m app.vision_response_cli RUN_ID PAGE [--attempt N] [--document-id SHA256]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from app.archive_store import ArchiveStore
from app.config import Settings


async def _raw_response(store: ArchiveStore, run_id: UUID, page: int, attempt: int,
                        document_id: str | None) -> bytes:
    if document_id is None:
        matched = []
        suffix = f'/runs/{run_id}/metadata.json'
        async for key in store.iter_manifest_keys():
            if key.endswith(suffix):
                matched.append(key.split('/', 1)[0])
                if len(matched) > 1:
                    raise ValueError('More than one matching run ID in the archive')
        if not matched:
            raise LookupError('Run metadata not found in the archive')
        document_id = matched[0]
    key = store.vision_call_key(document_id, run_id, page, response=True, attempt=attempt)
    result = await store.optional(key)
    if result is None:
        raise LookupError(f'No saved model response for page {page}, attempt {attempt}. '
                          'An HTTP failure or uncertain-outcome call may have no response.')
    return result.data


def main() -> None:
    parser = argparse.ArgumentParser(description='Print stored vision model content verbatim; no OpenRouter calls.')
    parser.add_argument('run_id', type=UUID)
    parser.add_argument('page', type=int)
    parser.add_argument('--attempt', type=int, default=1, choices=(1, 2, 3))
    parser.add_argument('--document-id', help='Optional original PDF SHA-256; avoids scanning archive manifests.')
    args = parser.parse_args()
    if not 1 <= args.page <= 10000:
        parser.error('page must be in 1..10000')
    if args.document_id is not None and (len(args.document_id) != 64 or
                                         any(c not in '0123456789abcdef' for c in args.document_id)):
        parser.error('--document-id must be a lowercase 64-character SHA-256')
    try:
        result = asyncio.run(_raw_response(ArchiveStore(Settings()), args.run_id,
                                          args.page, args.attempt, args.document_id))
    except (LookupError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')
    sys.stdout.buffer.write(result)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
