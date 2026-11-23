/** Backend contract and local-only validation. No external runtime dependencies. */
export type Parser = 'pypdf' | 'gemini' | 'gpt5_mini' | 'vision_figures'
export type CacheState = 'miss' | 'shared' | 'hit'
export type Status = 'queued' | 'processing' | 'summarizing' | 'partial' | 'completed' | 'failed'
export type Page = { page: number; markdown: string }
export type ProviderDiagnostic = Partial<Record<
  'phase' | 'kind' | 'requested_model' | 'response_model' | 'finish_reason' |
  'native_finish_reason' | 'response_id', string
>> & Partial<Record<
  'page' | 'pages_total' | 'max_tokens' | 'prompt_tokens' | 'completion_tokens' |
  'total_tokens' | 'http_status' | 'attempt' | 'max_attempts', number
>>
export type DocumentError = { code: string; message: string; diagnostic?: ProviderDiagnostic | null }
export type DocumentStatus = {
  id: string
  filename: string
  parser: Parser
  status: Status
  page_count: number | null
  summary: string | null
  cache: CacheState
  error: DocumentError | null
  trace_id: string | null
  upload_id: string | null
}
export type AcceptedDocument = {
  id: string
  filename: string
  parser: Parser
  status: 'queued' | 'completed'
  cache: CacheState
}

export const FILE_LIMITS = {
  maxFiles: 5,
  maxZipPdfs: 20,
  maxFileBytes: 20 * 1024 * 1024,
  maxBatchBytes: 50 * 1024 * 1024,
} as const

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)
export const isParser = (v: unknown): v is Parser =>
  v === 'pypdf' || v === 'gemini' || v === 'gpt5_mini' || v === 'vision_figures'
const isStatus = (v: unknown): v is Status =>
  v === 'queued' || v === 'processing' || v === 'summarizing' || v === 'partial' || v === 'completed' || v === 'failed'
const isCache = (v: unknown): v is CacheState => v === 'miss' || v === 'shared' || v === 'hit'
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const traceIdPattern = /^[0-9a-f]{32}$/i

/** Never trust or embed unvalidated OpenTelemetry IDs in links. */
export function validTraceId(value: unknown): string | null {
  return typeof value === 'string' && traceIdPattern.test(value) && !/^0{32}$/.test(value)
    ? value.toLowerCase() : null
}

function validUploadId(value: unknown): string | null {
  return typeof value === 'string' && uuidPattern.test(value) ? value.toLowerCase() : null
}
const diagnosticText = ['phase', 'kind', 'requested_model', 'response_model', 'finish_reason', 'native_finish_reason', 'response_id'] as const
const diagnosticNumbers = ['page', 'pages_total', 'max_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'http_status', 'attempt', 'max_attempts'] as const
const safeToken = /^[a-zA-Z0-9_./:+-]+$/

function parseDiagnostic(value: unknown): ProviderDiagnostic | null {
  if (!isObject(value)) return null
  const out: Record<string, string | number> = {}
  for (const key of diagnosticText) {
    const item = value[key]
    if (typeof item === 'string' && item.length <= 120 && safeToken.test(item)) out[key] = item
  }
  for (const key of diagnosticNumbers) {
    const item = value[key]
    if (typeof item === 'number' && Number.isInteger(item) && item >= 0 && item <= 1e9) out[key] = item
  }
  return Object.keys(out).length > 0 ? out : null
}

export function validateFiles(files: ReadonlyArray<{ name: string; size: number }>): string | null {
  if (!files.length) return 'Choose at least one PDF or one ZIP archive.'
  if (files.some(file => file.name.toLowerCase().endsWith('.zip'))) {
    if (files.length !== 1) return 'Upload a ZIP archive separately from PDF files.'
    const archive = files[0]
    if (!Number.isFinite(archive.size) || archive.size <= 0) return 'The ZIP archive is empty.'
    if (archive.size > FILE_LIMITS.maxBatchBytes) return 'The ZIP archive must be 50 MiB or less.'
    return null
  }
  if (files.length > FILE_LIMITS.maxFiles) return `Choose no more than ${FILE_LIMITS.maxFiles} PDFs at a time.`
  let total = 0
  for (const file of files) {
    if (!file.name.toLowerCase().endsWith('.pdf')) return `${file.name}: only PDF files or a ZIP archive are supported.`
    if (!Number.isFinite(file.size) || file.size <= 0) return `${file.name}: file is empty.`
    if (file.size > FILE_LIMITS.maxFileBytes) return `${file.name}: maximum file size is 20 MiB.`
    total += file.size
  }
  if (total > FILE_LIMITS.maxBatchBytes) return 'The total upload must be 50 MiB or less.'
  return null
}

export function parseUploadResponse(value: unknown, expectedCount: number | { min: number; max: number }): AcceptedDocument[] {
  const count = isObject(value) && Array.isArray(value.documents) ? value.documents.length : 0
  const validCount = typeof expectedCount === 'number' ? count === expectedCount :
    count >= expectedCount.min && count <= expectedCount.max
  if (!isObject(value) || !Array.isArray(value.documents) || !validCount) {
    throw new Error('The server returned an unexpected upload response.')
  }
  const ids = new Set<string>()
  return value.documents.map((item: unknown) => {
    if (!isObject(item) || typeof item.id !== 'string' || !uuidPattern.test(item.id) ||
        typeof item.filename !== 'string' || !isParser(item.parser) || (item.status !== 'queued' && item.status !== 'completed') ||
        (item.cache !== undefined && !isCache(item.cache))) {
      throw new Error('The server returned an invalid document ID or status.')
    }
    if (ids.has(item.id)) throw new Error('The server returned duplicate document IDs.')
    ids.add(item.id)
    return { id: item.id, filename: item.filename, parser: item.parser, status: item.status, cache: isCache(item.cache) ? item.cache : 'miss' }
  })
}

export function parseDocumentStatus(value: unknown, id: string): DocumentStatus {
  if (!isObject(value) || value.id !== id || typeof value.filename !== 'string' ||
      !isParser(value.parser) || !isStatus(value.status) ||
      (value.cache !== undefined && !isCache(value.cache)) ||
      (value.page_count !== null && value.page_count !== undefined &&
        (!Number.isInteger(value.page_count) || (value.page_count as number) < 1)) ||
      (value.summary !== null && value.summary !== undefined && typeof value.summary !== 'string')) {
    throw new Error('The server returned an invalid document status.')
  }
  const error = value.error
  if (error != null && (!isObject(error) || typeof error.code !== 'string' || typeof error.message !== 'string')) {
    throw new Error('The server returned an invalid document error.')
  }
  if (value.status === 'partial' && (!Number.isInteger(value.page_count as number) || !isObject(error))) {
    throw new Error('The server reported partial results without pages and an error.')
  }
  if (value.status === 'completed' && (typeof value.summary !== 'string' || !value.summary.trim())) {
    throw new Error('The server reported completion without a summary.')
  }
  return {
    id,
    filename: value.filename,
    parser: value.parser,
    status: value.status,
    page_count: typeof value.page_count === 'number' ? value.page_count : null,
    summary: typeof value.summary === 'string' ? value.summary : null,
    cache: isCache(value.cache) ? value.cache : 'miss',
    trace_id: validTraceId(value.trace_id),
    upload_id: validUploadId(value.upload_id),
    error: error == null ? null : {
      code: (error as Record<string, string>).code,
      message: (error as Record<string, string>).message,
      diagnostic: parseDiagnostic((error as Record<string, unknown>).diagnostic),
    },
  }
}

export function parsePages(value: unknown, id: string, expectedCount: number | null): Page[] {
  if (!isObject(value) || value.id !== id || !Array.isArray(value.pages) || !value.pages.length) {
    throw new Error('The server returned invalid page data.')
  }
  if (expectedCount != null && value.pages.length !== expectedCount) {
    throw new Error('The page count does not match the document metadata.')
  }
  return value.pages.map((page: unknown, i: number): Page => {
    if (!isObject(page) || page.page !== i + 1 || typeof page.markdown !== 'string') {
      throw new Error('The document pages are missing or out of order.')
    }
    return { page: page.page as number, markdown: page.markdown }
  })
}

/** Display actionable page-limit errors without exposing internal error codes in the UI. */
export function formatProcessingError(error: DocumentError): string {
  if (error.code === 'AI_PAGE_LIMIT_EXCEEDED') {
    return `Document too long for AI extraction. ${error.message} Split the PDF into smaller files or increase MAX_AI_PAGES_PER_DOCUMENT.`
  }
  if (error.code === 'PAGE_LIMIT_EXCEEDED') {
    return `Document too long to process. ${error.message} Split the PDF into smaller files or increase MAX_PAGES_PER_DOCUMENT.`
  }
  const info = error.diagnostic
  if (info && error.code.startsWith('PROVIDER_')) {
    const step = info.phase === 'ai_pdf' ? 'AI PDF extraction' : info.phase === 'image_ocr' ? 'Image OCR' :
      info.phase === 'summarization' ? 'Summarization' : 'AI processing'
    const location = info.page ? ` on page ${info.page}${info.pages_total ? ` of ${info.pages_total}` : ''}` : ''
    const reason = error.code === 'PROVIDER_TRUNCATED' ? 'model output reached the token allowance' :
      error.code === 'PROVIDER_INCOMPLETE' ? `model did not finish normally${info.finish_reason ? ` (reason: ${info.finish_reason})` : ''}` :
      info.kind === 'timeout' ? 'OpenRouter request timed out' :
      info.http_status === 429 ? 'OpenRouter rate limit (HTTP 429)' :
      info.http_status ? `OpenRouter returned HTTP ${info.http_status}` : error.message
    return `${step} failed${location}: ${reason}.`
  }
  return `${error.code}: ${error.message}`
}

/** Only allowlisted, bounded diagnostic fields appear in the expandable UI. */
export function diagnosticEntries(info: ProviderDiagnostic | null | undefined): Array<[string, string]> {
  if (!info) return []
  const fields: Array<[keyof ProviderDiagnostic, string]> = [
    ['phase', 'Stage'], ['page', 'Page'], ['pages_total', 'Pages in PDF'],
    ['requested_model', 'Requested model'], ['response_model', 'Response model'],
    ['finish_reason', 'Finish reason'], ['native_finish_reason', 'Provider finish reason'],
    ['max_tokens', 'Requested output tokens'], ['prompt_tokens', 'Input tokens'],
    ['completion_tokens', 'Output tokens'], ['total_tokens', 'Total tokens'],
    ['http_status', 'HTTP status'], ['kind', 'Failure type'],
    ['attempt', 'Request attempt'], ['max_attempts', 'Maximum attempts'],
    ['response_id', 'Response ID'],
  ]
  return fields.flatMap(([key, label]): Array<[string, string]> =>
    info[key] === undefined ? [] : [[label, String(info[key])]])
}

export function apiError(status: number, value: unknown): string {
  if (isObject(value) && isObject(value.error) &&
      typeof value.error.code === 'string' && typeof value.error.message === 'string') {
    return formatProcessingError(value.error as { code: string; message: string })
  }
  return `Request failed (HTTP ${status}).`
}
