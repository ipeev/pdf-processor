# Full-text search — PDF Processor 0.12.8

The **Search** navigation page provides lexical full-text retrieval over **previously extracted PDF pages**. It uses OpenSearch BM25 and requires **no OpenRouter key** for indexing or querying. Image contents that were not converted to text are not searchable. This is not semantic/embedding search.

## Data model and consistency

- **Source of truth:** permanent RustFS documents, run manifests and immutable `pages.json` extraction artifacts. No extracted text is stored authoritatively in OpenSearch.
- **Derived index:** `pdf-passages-v1` (alias `pdf-passages`), strict schema. Search documents contain original PDF SHA-256 (`document_id`), processing run UUID, extraction generation, parser, page, local passage index, filename, text, and a `primary` flag.
- **Passages:** sequential, non-overlapping text spans of up to ~220 whitespace-delimited words. Passage boundaries respect Markdown headings and page boundaries. Ordinary figure Markdown image references are omitted. The page number and source run are preserved. No OCR/LLM request is made to prepare the index.
- **Run selection:** every terminal run with committed extraction (`completed` **or** `partial`) is indexed. The canonical document's **preferred completed run** is primary when available. If none is available, the latest completed run is used; if no completed run exists, the latest partial extraction is used. This selection is derived, not stored in RustFS beyond the existing preferred-run field.
- **Lifecycle:** the worker refreshes the derived passages after publishing a completed or partial run. Changing preferred run or renaming a document triggers document reindexing. Deletion removes indexed text before deleting the archive; deleting an individual run reindexes any remaining extraction(s). **If incremental indexing fails, the archive result remains authoritative:** a later offline rebuild repairs the index. As with other derived indexes, do not treat search results as an archive backup.

Indexing all runs means two processing methods may contribute similar text. Search still returns **one top-level result per canonical PDF**. In **Preferred extraction** mode only primary passages are queried; in **All extractions**, the same text from different runs may appear as separate passages with distinct parser/run links. Search deduplicates results *at the PDF level*, not across different original PDFs.

## Ranking and results

BM25 ranks individual passages. Queries support **All terms**, **Any term**, and **Exact phrase**. Exact phrases receive an additional boost for multi-term searches. OpenSearch field collapsing on the keyword `document_id` returns the highest-ranked passage's PDF once. The top three inner hits are shown by default, with an endpoint to load additional matching passages. The number of matching passages is **not** the number of documents or the number of distinct pages. OpenSearch raw `_score` values are *relative*, not confidence percentages; the UI orders results but does not imply an absolute confidence rating. When sorting by newest, the latest extraction timestamp drives document order.

Pagination uses bounded `offset` and `limit` (maximum 20 documents per request, maximum offset 2000). The UI deliberately does **not** display an exact total-document count, since OpenSearch collapse does not return one. Results may move between pages if indexing occurs during pagination. Treat searches as a current read model, not a snapshot-isolated audit.

The snippet highlighter emits markers that the React component parses into **text nodes** and `<mark>` elements. Never render returned text using `innerHTML` or `dangerouslySetInnerHTML`.

## Initial indexing / repair

From a trusted, *idle* Linux deployment with the app running:

```bash
# Create/verify passage index; no PDF processing.
docker compose exec -T api python -m app.search_cli init

# Rebuild all already-extracted text from RustFS; zero OpenRouter calls.
docker compose exec -T api python -m app.search_cli rebuild
```

**Important:** rebuild requires an empty Redis processing queue and takes the same exclusive archive coordination lock as archive maintenance. It replaces **only** the derived `pdf-passages-v1` index, not RustFS originals, metadata, or paid extraction artifacts. For substantial archives, allow time and disk headroom for OpenSearch indexing. If a rebuild is interrupted, rerun the same command; do not rerun PDF extraction. The `rebuild` command clears orphan hits from deleted documents and runs.

Search initialization happens lazily on the first query/index update; until the initial rebuild, already-processed documents may not appear. If a search fails with `SEARCH_UNAVAILABLE`, inspect OpenSearch logs and perform an idle rebuild. Search isn't a substitute for verifying RustFS integrity or taking archive backups.

## HTTP API

All routes are under `/api/v1`. Search text is carried in a **POST JSON body**, not a query-string URL. Clients cannot submit OpenSearch DSL, scripts or arbitrary index names.

**Search grouped PDFs:** `POST /api/v1/search`

```json
{
  "query": "neural network architecture",
  "match": "all",
  "scope": "preferred",
  "parser": null,
  "sort": "relevance",
  "offset": 0,
  "limit": 10
}
```

Response example (example IDs are synthetic):

```json
{
  "items": [
    {
      "document_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "filename": "paper.pdf",
      "score": 14.8,
      "matching_passages": 3,
      "passages": [{
        "run_id": "11111111-1111-4111-8111-111111111111",
        "parser": "pypdf",
        "page": 4,
        "passage_index": 2,
        "snippet": "... <mark>neural network architecture</mark> ...",
        "score": 14.8
      }]
    }
  ],
  "has_more": false,
  "offset": 0,
  "total_documents": null
}
```

**More matches within one PDF:** `POST /api/v1/search/{document_id}/matches` with the same body (adjust `offset` and `limit` to page through matching *passages*, up to 20 per call). It returns `items`, an exact passage `total` for the given request scope, and `has_more`. The original document can be opened at `/library/{document_id}`; a passage links to `/runs/{run_id}?page={page}`.

`match` = `all | any | phrase`; `scope` = `preferred | all`; `sort` = `relevance | newest`; `parser` may be any current parser identifier or null. Queries are 2–200 characters after trimming. No query goes to OpenRouter. Application index failures return a bounded `SEARCH_UNAVAILABLE` (503), not a fabricated empty result.

## Deployment and privacy

Search indexes contain **plaintext extracted document content**. They are sensitive derived data: keep OpenSearch on the private internal Compose network, protect backups, and do not publish a search port. This project has no multi-user authentication or data ACLs. It is intended only for the same trusted local deployment boundary as the other features.

## Remaining limitations and possible improvements

- Reindexing currently replaces the passages for a document; concurrent indexing can temporarily produce incomplete or stale search results. An idle full rebuild is the authoritative repair path. Future versions may introduce atomic index-generation swaps or version-based reconciliation.
- Plain whitespace token segmentation is intentionally simple; multi-column ordering, code blocks, mathematical expressions, and tables may produce imperfect excerpts. No image-search or caption-image matching is implemented.
- BM25 is lexical. Embeddings, hybrid lexical/vector retrieval, RAG, query expansion, and semantic duplicate detection are potential future features, not available in this release.
- Indexing every extraction can add storage costs and shift corpus-wide BM25 statistics. Raw scores should not be compared between different queries or index rebuild generations.
