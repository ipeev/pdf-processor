"""Bounded, read-only, full-text search API (no OpenSearch DSL from clients)."""
from fastapi import APIRouter, Request, Response
from app.archive_gate import guarded
from app.catalogue_models import DocumentID
from app.errors import ProcessingError
from app.fulltext_search import SearchRequest, SearchResult, PassageResult

router = APIRouter(prefix='/api/v1/search', tags=['full-text search'])


def dependencies(req):
    pipeline = getattr(req.app.state, 'pipeline_archive', None)
    index = getattr(req.app.state, 'passage_index', None)
    if not pipeline or not index:
        raise ProcessingError('SEARCH_UNAVAILABLE', 'Search is not initialized.', 503)
    return pipeline, index


@router.post('', response_model=SearchResult)
async def search(req: Request, response: Response, body: SearchRequest):
    pipeline, index = dependencies(req)
    response.headers['Cache-Control'] = 'no-store'
    return await guarded(pipeline, lambda: index.search(body))


@router.post('/{document_id}/matches', response_model=PassageResult)
async def matches(req: Request, response: Response, document_id: DocumentID, body: SearchRequest):
    pipeline, index = dependencies(req)
    async def operation():
        try:
            await pipeline.documents.get(document_id)
        except ProcessingError as exc:
            if exc.code == 'ARCHIVE_NOT_FOUND':
                raise ProcessingError('DOCUMENT_NOT_FOUND', 'Document does not exist.', 404) from exc
            raise
        return await index.passages(document_id, body)
    response.headers['Cache-Control'] = 'no-store'
    return await guarded(pipeline, operation)
