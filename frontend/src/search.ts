import { isDocumentId, isRunId } from './catalogue.ts'
import { isParser } from './contracts.ts'

export type SearchOptions = {
  query: string; match: 'all' | 'any' | 'phrase'; scope: 'preferred' | 'all';
  parser: string | null; sort: 'relevance' | 'newest'; offset: number; limit: number
}
export type Match = { run_id: string; parser: string; page: number; passage_index: number; snippet: string; score: number | null }
export type FoundDocument = {
  document_id: string; filename: string; score: number | null;
  matching_passages: number; passages: Match[]
}
export type SearchResponse = { items: FoundDocument[]; has_more: boolean; offset: number; total_documents: null }
export type MatchResponse = { items: Match[]; total: number; has_more: boolean }

function record(v: unknown): Record<string, unknown> {
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('Unexpected search response.')
  return v as Record<string, unknown>
}
function nonnegative(v: unknown): v is number { return typeof v === 'number' && Number.isFinite(v) && v >= 0 }
function count(v: unknown): v is number { return Number.isSafeInteger(v) && (v as number) >= 0 }
function score(v: unknown) { return v === null || nonnegative(v) }
export function parseMatch(value: unknown): Match {
  const v = record(value)
  if (!isRunId(v.run_id) || !isParser(v.parser) || !count(v.page) || v.page < 1 || !count(v.passage_index) ||
      typeof v.snippet !== 'string' || v.snippet.length > 5000 || !score(v.score)) throw new Error('Invalid matching passage.')
  return { run_id: v.run_id, parser: v.parser, page: v.page, passage_index: v.passage_index, snippet: v.snippet, score: v.score as number|null }
}
export function parseSearch(value: unknown): SearchResponse {
  const v = record(value)
  if (!Array.isArray(v.items) || v.items.length > 20 || typeof v.has_more !== 'boolean' || !count(v.offset) || v.total_documents !== null) throw new Error('Invalid search page.')
  const seen = new Set<string>()
  const items: FoundDocument[] = v.items.map(raw => {
    const x = record(raw)
    if (!isDocumentId(x.document_id) || seen.has(x.document_id) || typeof x.filename !== 'string' ||
        x.filename.length > 1024 || !score(x.score) || !count(x.matching_passages) || x.matching_passages < 1 ||
        !Array.isArray(x.passages) || x.passages.length > 3) throw new Error('Invalid search document.')
    seen.add(x.document_id)
    return { document_id: x.document_id, filename: x.filename, score: x.score as number|null,
      matching_passages: x.matching_passages, passages: x.passages.map(parseMatch) }
  })
  return { items, has_more: v.has_more, offset: v.offset, total_documents: null }
}
export function parseMatches(value: unknown): MatchResponse {
  const v = record(value)
  if (!Array.isArray(v.items) || v.items.length > 20 || !count(v.total) || typeof v.has_more !== 'boolean') throw new Error('Invalid matching passages.')
  return { items: v.items.map(parseMatch), total: v.total, has_more: v.has_more }
}

// OpenSearch provides highlight markers; never inject the snippet as HTML.
// Literal angle brackets from PDFs remain escaped by React as plain text.
export function highlightedSegments(source: string): { text: string; highlighted: boolean }[] {
  const parts = source.split(/(<mark>|<\/mark>)/g)
  let marked = false
  const result: {text:string;highlighted:boolean}[] = []
  for (const part of parts) {
    if (part === '<mark>') marked = true
    else if (part === '</mark>') marked = false
    else if (part) result.push({text: part, highlighted: marked})
  }
  return result
}
