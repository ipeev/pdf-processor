import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  validateFiles, parseUploadResponse, parseDocumentStatus, parsePages, apiError, formatProcessingError, diagnosticEntries, validTraceId,
} from '../src/contracts.ts'

const ID = 'a2e8e817-273d-4e21-b3bc-4cc0919b8352'
const ID2 = 'f9b56c42-0e9d-4a9d-91cc-42d13517ab0e'
const uploaded = (id = ID) => ({ id, filename: 'test.pdf', parser: 'pypdf', status: 'queued', cache: 'miss' })
const status = (changes = {}) => ({
  id: ID, filename: 'test.pdf', parser: 'pypdf', status: 'processing',
  page_count: null, summary: null, error: null, ...changes,
})
const pages = (items = [{ page: 1, markdown: 'Hello' }]) => ({ id: ID, pages: items })

// File selection and limits mirror backend defaults.
test('rejects no files', () => assert.match(validateFiles([]), /Choose/))
test('accepts uppercase PDF extension', () => assert.equal(validateFiles([{ name: 'REPORT.PDF', size: 120 }]), null))
test('rejects unsupported extension', () => assert.match(validateFiles([{ name: 'test.txt', size: 10 }]), /PDF/))
test('rejects empty files', () => assert.match(validateFiles([{ name: 'test.pdf', size: 0 }]), /empty/))
test('rejects too many files', () => assert.match(validateFiles(Array.from({length: 6}, (_, i) => ({name: `${i}.pdf`, size: 1}))), /5/))
test('accepts exactly 5 files', () => assert.equal(validateFiles(Array.from({length: 5}, (_, i) => ({name: `${i}.pdf`, size: 1}))), null))
test('rejects oversized file', () => assert.match(validateFiles([{ name: 'test.pdf', size: 20 * 1024 * 1024 + 1 }]), /20 MiB/))
test('rejects excessive total', () => assert.match(validateFiles(Array.from({ length: 3 }, (_, i) => ({ name: `${i}.pdf`, size: 19 * 1024 * 1024 }))), /50 MiB/))


test('accepts one ZIP archive', () => assert.equal(validateFiles([{ name: 'BUNDLE.ZIP', size: 1024 }]), null))
test('rejects mixed ZIP and PDF files', () => assert.match(validateFiles([{ name: 'a.zip', size: 1024 }, { name: 'b.pdf', size: 512 }]), /separately/))
test('rejects oversized ZIP archive', () => assert.match(validateFiles([{ name: 'a.zip', size: 51 * 1024 * 1024 }]), /50 MiB/))
test('accepts ZIP expansion into multiple document IDs', () => assert.equal(parseUploadResponse({ documents: [uploaded(ID), uploaded(ID2)] }, { min: 1, max: 20 }).length, 2))
test('rejects ZIP expansion exceeding 20 documents', () => assert.throws(() => parseUploadResponse({ documents: Array.from({ length: 21 }, () => uploaded()) }, { min: 1, max: 20 }), /unexpected/))

test('accepts HTTP 202 document result shape', () => assert.deepEqual(parseUploadResponse({ documents: [uploaded()] }, 1)[0], uploaded()))
test('accepts multiple independent UUIDs', () => assert.equal(parseUploadResponse({ documents: [uploaded(ID), uploaded(ID2)] }, 2).length, 2))
test('rejects mismatched upload count', () => assert.throws(() => parseUploadResponse({ documents: [uploaded()] }, 2), /unexpected/))
test('rejects duplicate document IDs', () => assert.throws(() => parseUploadResponse({ documents: [uploaded(), uploaded()] }, 2), /duplicate/))
test('rejects invalid UUID', () => assert.throws(() => parseUploadResponse({ documents: [uploaded('oops')] }, 1), /invalid/))
test('rejects wrong parser name', () => assert.throws(() => parseUploadResponse({ documents: [{ ...uploaded(), parser: 'foobar' }] }, 1), /invalid/))
test('accepts completed cache hit', () => assert.equal(parseUploadResponse({ documents: [{ ...uploaded(), status: 'completed', cache: 'hit' }] }, 1)[0].cache, 'hit'))

test('parses queued status', () => assert.equal(parseDocumentStatus(status({status: 'queued'}), ID).status, 'queued'))
test('parses processing status', () => assert.equal(parseDocumentStatus(status(), ID).status, 'processing'))
test('parses completed with summary', () => assert.equal(parseDocumentStatus(status({ status: 'completed', summary: 'Summary', page_count: 1 }), ID).summary, 'Summary'))
test('parses failure and its error', () => assert.equal(parseDocumentStatus(status({ status: 'failed', error: {code: 'NO_EXTRACTABLE_TEXT', message: 'No text'} }), ID).error.code, 'NO_EXTRACTABLE_TEXT'))
test('rejects completion with empty summary', () => assert.throws(() => parseDocumentStatus(status({ status: 'completed', summary: '   ' }), ID), /without a summary/))
test('rejects unexpected document ID', () => assert.throws(() => parseDocumentStatus(status(), ID2), /invalid/))
test('rejects unknown status', () => assert.throws(() => parseDocumentStatus(status({ status: 'running' }), ID), /invalid/))
test('rejects invalid page count', () => assert.throws(() => parseDocumentStatus(status({ page_count: -1 }), ID), /invalid/))

test('parses an ordered document page list', () => assert.equal(parsePages(pages([{ page: 1, markdown: 'A' }, { page: 2, markdown: '' }]), ID, 2).length, 2))
test('rejects missing page', () => assert.throws(() => parsePages(pages([{ page: 2, markdown: 'A' }]), ID, 1), /out of order/))
test('rejects wrong page count', () => assert.throws(() => parsePages(pages(), ID, 2), /count/))
test('rejects foreign page response', () => assert.throws(() => parsePages(pages(), ID2, 1), /invalid/))
test('rejects missing pages', () => assert.throws(() => parsePages(pages([]), ID, 0), /invalid/))
test('reports sanitized backend error code and message', () => assert.equal(apiError(503, { error: { code: 'REDIS_UNAVAILABLE', message: 'Redis down' }}), 'REDIS_UNAVAILABLE: Redis down'))
test('falls back for unexpected error body', () => assert.equal(apiError(502, '<html>Bad Gateway</html>'), 'Request failed (HTTP 502).'))
test('renders configured AI page cap as a friendly status error', () => {
  const message = formatProcessingError({code: 'AI_PAGE_LIMIT_EXCEEDED', message: 'This PDF has 243 pages; the configured AI extraction limit is 200 pages.'})
  assert.match(message, /Document too long for AI extraction/)
  assert.match(message, /243 pages/)
  assert.match(message, /200 pages/)
  assert.match(message, /MAX_AI_PAGES_PER_DOCUMENT/)
  assert.doesNotMatch(message, /AI_PAGE_LIMIT_EXCEEDED/)
})
test('renders general page cap as a friendly status error', () => {
  const message = formatProcessingError({code: 'PAGE_LIMIT_EXCEEDED', message: 'This PDF has 201 pages; the configured document limit is 200 pages.'})
  assert.match(message, /Document too long to process/)
  assert.match(message, /201 pages/)
  assert.match(message, /MAX_PAGES_PER_DOCUMENT/)
  assert.doesNotMatch(message, /PAGE_LIMIT_EXCEEDED/)
})
test('formats page limits in upload and retry API errors too', () => {
  const message = apiError(422, {error: {code: 'PAGE_LIMIT_EXCEEDED', message: 'This PDF has 201 pages; the configured document limit is 200 pages.'}})
  assert.match(message, /Document too long to process/)
})

test('formats AI OCR truncation with the failing page and total', () => {
  const message = formatProcessingError({
    code: 'PROVIDER_TRUNCATED', message: 'Model output hit the configured token limit.',
    diagnostic: { phase: 'image_ocr', page: 12, pages_total: 80, finish_reason: 'length' },
  })
  assert.match(message, /Image OCR failed on page 12 of 80/)
  assert.match(message, /token allowance/)
})

test('formats unexpected provider finish reason and HTTP 429 usefully', () => {
  assert.match(formatProcessingError({
    code: 'PROVIDER_INCOMPLETE', message: 'Model did not finish normally.',
    diagnostic: { phase: 'ai_pdf', page: 4, pages_total: 25, finish_reason: 'content_filter' },
  }), /AI PDF extraction failed on page 4 of 25.*content_filter/)
  assert.match(formatProcessingError({
    code: 'PROVIDER_UNAVAILABLE', message: 'OpenRouter request failed.',
    diagnostic: { phase: 'summarization', http_status: 429 },
  }), /Summarization failed.*rate limit \(HTTP 429\)/)
})

test('parses only bounded technical diagnostic keys without raw provider bodies', () => {
  const received = parseDocumentStatus(status({status: 'failed', error: {
    code: 'PROVIDER_TRUNCATED', message: 'Hit limit',
    diagnostic: { phase: 'image_ocr', page: 3, pages_total: 10, max_tokens: 32768,
      finish_reason: 'length', raw_body: 'SECRET', response_id: 'unsafe\nPRIVATE' },
  }}), ID)
  assert.equal(received.error.diagnostic.page, 3)
  assert.equal(diagnosticEntries(received.error.diagnostic).find(([name]) => name === 'Requested output tokens')?.[1], '32768')
  assert.equal(diagnosticEntries(received.error.diagnostic).some(([, value]) => value.includes('SECRET')), false)
  assert.equal('raw_body' in received.error.diagnostic, false)
  assert.equal('response_id' in received.error.diagnostic, false)
})

test('older error shape without diagnostics remains compatible', () => {
  const received = parseDocumentStatus(status({status: 'failed', error: {code: 'INVALID_PDF', message: 'bad PDF'}}), ID)
  assert.equal(received.error.diagnostic, null)
  assert.equal(diagnosticEntries(received.error.diagnostic).length, 0)
})

test('frontend version label is derived from package version and matches backend', () => {
  const packageVersion = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  const pyproject = readFileSync(new URL('../../backend/pyproject.toml', import.meta.url), 'utf8')
  assert.match(app, /version-mark">v\{appVersion\}/)
  assert.doesNotMatch(app, /version-mark">v0\.9\.0/)
  // Python package metadata uses the PEP 440 spelling for SemVer pre-releases.
  const pythonVersion = packageVersion.replace(/-alpha\.(\d+)\.(\d+)$/, 'a$1.post$2').replace(/-alpha\.(\d+)$/, 'a$1').replace(/-beta\.(\d+)\.(\d+)$/, 'b$1.post$2').replace(/-beta\.(\d+)$/, 'b$1').replace(/-rc\.(\d+)\.(\d+)$/, 'rc$1.post$2').replace(/-rc\.(\d+)$/, 'rc$1')
  assert.match(pyproject, new RegExp(`version = "${pythonVersion.replaceAll('.', '\\.')}"`))
})


test('parses a partial result with an actionable error', () => assert.equal(
  parseDocumentStatus(status({ status: 'partial', page_count: 2,
    error: { code: 'PROVIDER_TRUNCATED', message: 'Summary incomplete' } }), ID).status,
  'partial'))
test('rejects partial results without an error or pages', () => assert.throws(
  () => parseDocumentStatus(status({ status: 'partial', page_count: 2 }), ID), /partial results/))
test('accepts the summarizing intermediate state', () => assert.equal(
  parseDocumentStatus(status({ status: 'summarizing', page_count: 2 }), ID).status, 'summarizing'))

test('accepts in-flight shared upload', () => assert.equal(parseUploadResponse({documents: [{...uploaded(), cache: 'shared'}]}, 1)[0].cache, 'shared'))
test('rejects unknown cache state', () => assert.throws(() => parseDocumentStatus(status({cache: 'mystery'}), ID), /invalid/))
test('parses completed cached result', () => assert.equal(parseDocumentStatus(status({status: 'completed', cache: 'hit', summary: 'Done', page_count: 1}), ID).cache, 'hit'))

const TRACE = '4ed7e7ceeee61c6e1ca34ecf3fdd71a5'
const UPLOAD = '89c5771a-84f2-48ba-a63f-b29847272d13'

test('Trace ID and Upload ID survive queued, processing, summarizing and success statuses', () => {
  for (const state of ['queued', 'processing', 'summarizing', 'completed']) {
    const received = parseDocumentStatus(status({
      status: state, trace_id: TRACE.toUpperCase(), upload_id: UPLOAD,
      ...(state === 'completed' ? { summary: 'Successfully summarized', page_count: 1 } : {}),
    }), ID)
    assert.equal(received.trace_id, TRACE)
    assert.equal(received.upload_id, UPLOAD)
    assert.equal(received.status, state)
  }
})

test('Trace ID survives failed and partial results with original diagnostics', () => {
  for (const state of ['failed', 'partial']) {
    const received = parseDocumentStatus(status({
      status: state, trace_id: TRACE, upload_id: UPLOAD, page_count: state === 'partial' ? 2 : null,
      error: { code: 'PROVIDER_TRUNCATED', message: 'Model limit', diagnostic: { page: 6, phase: 'image_ocr' } },
    }), ID)
    assert.equal(received.trace_id, TRACE)
    assert.equal(received.upload_id, UPLOAD)
    assert.equal(received.error.diagnostic.page, 6)
  }
})

test('ZIP members may share a trace and keep independent document IDs', () => {
  const one = parseDocumentStatus(status({ trace_id: TRACE, upload_id: UPLOAD }), ID)
  const two = parseDocumentStatus({ ...status({ trace_id: TRACE, upload_id: UPLOAD }), id: ID2 }, ID2)
  assert.equal(one.trace_id, two.trace_id)
  assert.equal(one.upload_id, two.upload_id)
  assert.notEqual(one.id, two.id)
})

test('Trace metadata is safely nullable for legacy and malformed backend values', () => {
  for (const state of ['queued', 'processing', 'summarizing', 'completed', 'partial', 'failed']) {
    const extra = state === 'completed' ? { summary: 'Summary', page_count: 1 } :
      state === 'partial' ? { page_count: 1, error: { code: 'SUMMARY', message: 'Oops' } } :
      state === 'failed' ? { error: { code: 'OCR', message: 'Oops' } } : {}
    const missing = parseDocumentStatus(status({ status: state, ...extra }), ID)
    assert.equal(missing.trace_id, null)
    assert.equal(missing.upload_id, null)
    const invalid = parseDocumentStatus(status({ status: state, ...extra,
      trace_id: 'javascript:alert(1)', upload_id: 'not-a-uuid' }), ID)
    assert.equal(invalid.trace_id, null)
    assert.equal(invalid.upload_id, null)
  }
  assert.equal(validTraceId('0'.repeat(32)), null)
  assert.equal(validTraceId(TRACE.toUpperCase()), TRACE)
  assert.equal(validTraceId(TRACE + 'extra'), null)
})
