import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { PARSERS, STATUS_LABELS, processingSteps, formatBytes, markdownFilename, pagesAsMarkdown, safeMarkdownUrl } from '../src/ui.ts'

const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')

test('fourth parser is available for normal uploads', () => {
  assert.deepEqual(PARSERS.map(item => item.id), ['pypdf', 'gemini', 'gpt5_mini', 'vision_figures'])
  assert.notEqual(PARSERS.at(-1).disabled, true)
  assert.match(source, /disabled=\{uploading \|\| item.disabled === true\}/)
})
test('all six document states have labels', () =>
  assert.deepEqual(Object.keys(STATUS_LABELS).sort(), ['completed', 'failed', 'partial', 'processing', 'queued', 'summarizing']))
test('stage indicators show progress without a guessed percentage', () => {
  assert.deepEqual(processingSteps('queued').map(s => s.state), ['done', 'waiting', 'waiting'])
  assert.deepEqual(processingSteps('processing').map(s => s.state), ['done', 'current', 'waiting'])
  assert.deepEqual(processingSteps('summarizing').map(s => s.state), ['done', 'done', 'current'])
  assert.deepEqual(processingSteps('completed').map(s => s.state), ['done', 'done', 'done'])
})
test('partial result preserves extraction and marks summary failed', () =>
  assert.deepEqual(processingSteps('partial').map(s => s.state), ['done', 'done', 'error']))
test('failed extraction does not falsely report completed summary', () =>
  assert.deepEqual(processingSteps('failed').map(s => s.state), ['done', 'error', 'waiting']))
test('file sizes are human-readable', () => {
  assert.equal(formatBytes(2048), '2 KiB')
  assert.equal(formatBytes(2 * 1024 * 1024), '2.0 MiB')
  assert.equal(formatBytes(-1), '—')
})
test('download names cannot contain path traversal or control characters', () => {
  assert.equal(markdownFilename('../invoices\\2026.pdf', 'summary'), 'invoices_2026-summary.md')
  assert.equal(markdownFilename('invoice.PDF', 'pages'), 'invoice-pages.md')
  assert.equal(markdownFilename('...pdf', 'summary'), 'document-summary.md')
})
test('markdown download preserves ordered pages and empty-page markers', () => {
  assert.equal(pagesAsMarkdown([{ page: 1, markdown: 'Hello' }, { page: 2, markdown: '' }]),
    '# Page 1\n\nHello\n\n---\n\n# Page 2\n\n_(Blank page)_\n')
})
test('upload behavior still uses the existing API and never retries a POST automatically', () => {
  assert.match(source, /fetch\('\/api\/v1\/documents', \{ method: 'POST', body \}\)/)
  assert.match(source, /Do not resubmit automatically/)
  assert.match(source, /fetch\(`\/api\/v1\/documents\/\$\{encodeURIComponent\(id\)\}\/retry-summary`/)
  assert.match(source, /setTimeout\(async \(\) =>/)
})
test('new upload experience has drag/drop, parser cards, and accessible file removal', () => {
  assert.match(source, /onDrop=\{onDrop\}/)
  assert.match(source, /onDragOver=\{onDragOver\}/)
  assert.match(source, /type="radio"/)
  assert.match(source, /aria-label=\{`Remove \$\{file.name\}`\}/)
  assert.match(source, /role="alert"/)
})
test('result experience handles status, cache hits, pages, partial results and retry', () => {
  for (const part of ['cache === \'hit\'', 'cache === \'shared\'', 'retrySummary(selected.id)', 'role="tablist"', 'role="tabpanel"', '/markdown`}', '/summary`}']) {
    assert.ok(source.includes(part), `Missing ${part}`)
  }
})
test('untrusted document markdown is restricted and no raw HTML parser is used', () => {
  assert.match(source, /urlTransform=\{safeUrl\}/)
  assert.match(source, /noopener noreferrer/)
  assert.doesNotMatch(source, /rehypeRaw|dangerouslySetInnerHTML/)
})
test('Markdown URL policy rejects untrusted protocols', () => {
  assert.equal(safeMarkdownUrl('javascript:alert(1)', 'http://localhost:3000'), '')
  assert.equal(safeMarkdownUrl('data:text/html,payload', 'http://localhost:3000'), '')
  assert.equal(safeMarkdownUrl('https://example.org', 'http://localhost:3000'), 'https://example.org')
  assert.equal(safeMarkdownUrl('/docs', 'http://localhost:3000'), '/docs')
  assert.equal(safeMarkdownUrl('mailto:test@example.org', 'http://localhost:3000'), 'mailto:test@example.org')
})
test('dark responsive styling supports keyboard focus and reduced motion', () => {
  assert.match(css, /background:\s*#090d19/)
  assert.match(css, /focus-visible/)
  assert.match(css, /prefers-reduced-motion/)
  assert.match(css, /max-width:\s*600px/)
})

test('math-aware preview renders inline and display LaTeX without unsafe HTML', () => {
  assert.match(source, /import remarkMath from 'remark-math'/)
  assert.match(source, /import rehypeKatex from 'rehype-katex'/)
  assert.match(source, /import 'katex\/dist\/katex\.min\.css'/)
  assert.match(source, /remarkPlugins=\{\[remarkGfm, remarkMath\]\}/)
  assert.match(source, /normalizeDisplayMath\(text\)/)
  assert.match(source, /rehypePlugins=\{\[\[rehypeKatex, \{ trust: false, throwOnError: false/)
  assert.match(source, /maxExpand: 1000/)
  assert.match(source, /<Markdown text=\{selected\.summary\}/)
  assert.match(source, /<Markdown text=\{currentPage\.markdown\} imageRunId=/)
  assert.doesNotMatch(source, /rehypeRaw|dangerouslySetInnerHTML/)
})

test('math dependency tree uses patched KaTeX and prevents older transitive copies', () => {
  const manifest = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
  assert.equal(manifest.version, '0.12.3')
  assert.equal(manifest.dependencies['remark-math'], '^6.0.0')
  assert.equal(manifest.dependencies['rehype-katex'], '^7.0.1')
  assert.equal(manifest.dependencies.katex, '^0.18.11')
  assert.equal(manifest.overrides.katex, '$katex')
})

test('result preview alone has a VS Code near-black editor theme and scrollable math', () => {
  assert.match(css, /\.preview-wrap\s*\{[^}]*background:#181818/)
  assert.match(css, /\.preview-wrap \.preview-body\s*\{[^}]*background:#1e1e1e/)
  assert.match(css, /\.preview-wrap \.markdown \.(?:katex-display)[^}]*overflow-x:auto/)
  assert.match(css, /\.preview-wrap \.markdown \.(?:katex-error)/)
  assert.doesNotMatch(css, /\.results-panel\s*\{[^}]*background:#181818/)
  assert.doesNotMatch(css, /\.upload-panel\s*\{[^}]*background:#181818/)
})
