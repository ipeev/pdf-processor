import type { CacheState, Parser, Status } from './contracts.ts'
import { isParser } from './contracts.ts'

export const STATUSES: Status[] = ['queued', 'processing', 'summarizing', 'completed', 'partial', 'failed']
export const isRunId = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v)
export const isDocumentId = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{64}$/.test(v)
export type Route = { view: 'history' | 'upload' | 'library' | 'system' | 'missing' } | { view: 'run' | 'document'; id: string }
export function routeFor(path: string): Route {
  if (path === '/') return { view: 'history' }
  if (path === '/upload') return { view: 'upload' }
  if (path === '/library') return { view: 'library' }
  if (path === '/system') return { view: 'system' }
  const parts = path.split('/')
  if (parts.length === 3 && parts[1] === 'runs' && isRunId(parts[2])) return { view: 'run', id: parts[2] }
  if (parts.length === 3 && parts[1] === 'library' && isDocumentId(parts[2])) return { view: 'document', id: parts[2] }
  return { view: 'missing' }
}
export type RunRecord = {
  run_id: string; document_id: string; filename: string; parser: Parser; status: Status; cache: CacheState;
  page_count: number | null; created_at: string; updated_at: string; duration_ms: number | null;
  extraction_model: string | null; summary_model: string | null; application_version: string;
  cost_usd: number | null; has_extraction: boolean; has_summary: boolean
}
export type LibraryRecord = {
  document_id: string; display_name: string; filenames: string[]; file_size_bytes: number;
  page_count: number | null; preferred_run_id: string | null; created_at: string; updated_at: string; revision: number
}
export type CataloguePage<T> = { items: T[]; next_cursor: string | null }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid catalogue response.')
  return value as Record<string, unknown>
}
const str = (v: unknown): v is string => typeof v === 'string'
const date = (v: unknown): v is string => str(v) && Number.isFinite(Date.parse(v))
const nonnegative = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0
const nullableNumber = (v: unknown): v is number | null => v === null || nonnegative(v)
const nullableText = (v: unknown): v is string | null => v === null || str(v)
export function parseRun(value: unknown, expectedId?: string): RunRecord {
  const x = object(value)
  if (!isRunId(x.run_id) || (expectedId && x.run_id !== expectedId) || !isDocumentId(x.document_id) || !str(x.filename) ||
      !isParser(x.parser) || !STATUSES.includes(x.status as Status) ||
      !str(x.cache) || !['miss', 'hit', 'shared'].includes(x.cache) || !nullableNumber(x.page_count) || !date(x.created_at) || !date(x.updated_at) ||
      !nullableNumber(x.duration_ms) || !nullableNumber(x.cost_usd) || !nullableText(x.extraction_model) || !nullableText(x.summary_model) ||
      !str(x.application_version) || typeof x.has_extraction !== 'boolean' || typeof x.has_summary !== 'boolean') throw new Error('Invalid run metadata.')
  return { run_id: x.run_id, document_id: x.document_id, filename: x.filename, parser: x.parser as Parser, status: x.status as Status,
    cache: x.cache as CacheState, page_count: x.page_count, created_at: x.created_at, updated_at: x.updated_at, duration_ms: x.duration_ms,
    cost_usd: x.cost_usd, extraction_model: x.extraction_model, summary_model: x.summary_model, application_version: x.application_version,
    has_extraction: x.has_extraction, has_summary: x.has_summary }
}
export function parseLibrary(value: unknown, expectedId?: string): LibraryRecord {
  const x = object(value)
  if (!isDocumentId(x.document_id) || x.sha256 !== x.document_id || (expectedId && x.document_id !== expectedId) || !str(x.display_name) ||
      !Array.isArray(x.filenames) || !x.filenames.every(str) || !nonnegative(x.file_size_bytes) || !nullableNumber(x.page_count) ||
      !(x.preferred_run_id === null || isRunId(x.preferred_run_id)) || !date(x.created_at) || !date(x.updated_at) ||
      !nonnegative(x.revision) || !Number.isSafeInteger(x.revision) || x.revision < 1) throw new Error('Invalid library metadata.')
  return { document_id: x.document_id, display_name: x.display_name, filenames: x.filenames, file_size_bytes: x.file_size_bytes,
    page_count: x.page_count, preferred_run_id: x.preferred_run_id, created_at: x.created_at, updated_at: x.updated_at, revision: x.revision }
}
export function parseCataloguePage<T>(value: unknown, parse: (item: unknown) => T, key: (item: T) => string): CataloguePage<T> {
  const x = object(value)
  if (!Array.isArray(x.items) || x.items.length > 100 || !(x.next_cursor === null || (str(x.next_cursor) && x.next_cursor.length > 0))) throw new Error('Invalid catalogue page.')
  const unique = new Map<string, T>()
  for (const raw of x.items) { const item = parse(raw); unique.set(key(item), item) }
  return { items: [...unique.values()], next_cursor: x.next_cursor }
}
export type Filters = { filename: string; parser: string; status: string; from: string; to: string; sort: 'newest' | 'oldest' }
export const EMPTY_FILTERS: Filters = { filename: '', parser: '', status: '', from: '', to: '', sort: 'newest' }
export function listUrl(path: string, filters: Filters, cursor: string | null): string {
  const query = new URLSearchParams({ limit: '50', sort: filters.sort })
  if (filters.filename.trim()) query.set('filename', filters.filename.trim())
  if (filters.parser) query.set('parser', filters.parser)
  if (filters.status) query.set('status', filters.status)
  // datetime-local is browser-local time; explicit timezone offset is required by the API.
  if (filters.from) query.set('created_from', new Date(filters.from).toISOString())
  if (filters.to) query.set('created_to', new Date(filters.to).toISOString())
  if (filters.from && filters.to && new Date(filters.from) > new Date(filters.to)) throw new Error('Start date must not be after end date.')
  if (cursor) query.set('cursor', cursor)
  return `/api/v1${path}?${query}`
}
export function localDate(value: string): string { return new Date(value).toLocaleString() }
