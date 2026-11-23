import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { grafanaTraceUrl, copyIdentifier } from '../src/traceLinks.ts'
import { validTraceId } from '../src/contracts.ts'

const TRACE = '4ed7e7ceeee61c6e1ca34ecf3fdd71a5'
const DOC = 'a2e8e817-273d-4e21-b3bc-4cc0919b8352'
const ZIP = '89c5771a-84f2-48ba-a63f-b29847272d13'

test('Grafana 13 Explore link targets the existing tempo UID and correct trace', () => {
  const link = grafanaTraceUrl('http://127.0.0.1:3101/', TRACE)
  const url = new URL(link)
  assert.equal(url.origin, 'http://127.0.0.1:3101')
  assert.equal(url.pathname, '/explore')
  assert.equal(url.searchParams.get('schemaVersion'), '1')
  const panes = JSON.parse(url.searchParams.get('panes'))
  assert.equal(panes.trace.datasource, 'tempo')
  assert.equal(panes.trace.queries[0].queryType, 'traceId')
  assert.equal(panes.trace.queries[0].query, TRACE)
  assert.equal(panes.trace.queries[0].datasource.uid, 'tempo')
  assert.equal(panes.trace.range.from, 'now-7d')
  assert.equal(panes.trace.range.to, 'now')
})

test('Grafana link accepts subpaths, explicit UIDs, valid uppercase trace IDs', () => {
  const link = grafanaTraceUrl('https://grafana.example.org/grafana', TRACE.toUpperCase(), 'custom-Tempo_1')
  const url = new URL(link)
  assert.equal(url.pathname, '/grafana/explore')
  assert.equal(JSON.parse(url.searchParams.get('panes')).trace.queries[0].query, TRACE)
  assert.equal(JSON.parse(url.searchParams.get('panes')).trace.datasource, 'custom-Tempo_1')
})

test('No Grafana link when base URL, source UID or trace ID is unsafe', () => {
  for (const unsafe of ['', 'javascript:alert(1)', 'data:text/html,evil', 'file:///etc/passwd',
    'http://admin:password@localhost:3101', 'https://grafana.example/?api_key=SECRET',
    'https://grafana.example/#redirect', '/relative/path']) {
    assert.equal(grafanaTraceUrl(unsafe, TRACE), null, unsafe)
  }
  for (const badId of ['bad', '0'.repeat(32), TRACE + '../', null]) {
    assert.equal(grafanaTraceUrl('http://127.0.0.1:3101', badId), null)
    assert.equal(validTraceId(badId), null)
  }
  assert.equal(grafanaTraceUrl('https://grafana.example', TRACE, '../../evil'), null)
})

test('Copy uses native Clipboard API if available', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  let copied = ''
  try {
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async v => { copied = v } } } })
    assert.equal(await copyIdentifier(TRACE), true)
    assert.equal(copied, TRACE)
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'navigator', descriptor)
    else delete globalThis.navigator
  }
})

test('Copy falls back to legacy selection for non-secure HTTP pages', async () => {
  const navigatorDesc = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  const documentDesc = Object.getOwnPropertyDescriptor(globalThis, 'document')
  const selected = []
  let removed = false
  try {
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {} })
    Object.defineProperty(globalThis, 'document', { configurable: true, value: {
      createElement: () => ({ value: '', style: {}, setAttribute: () => {}, select() { selected.push(this.value) }, remove() { removed = true } }),
      body: { appendChild() {} },
      execCommand: name => name === 'copy',
    } })
    assert.equal(await copyIdentifier(DOC), true)
    assert.deepEqual(selected, [DOC])
    assert.ok(removed)
  } finally {
    if (navigatorDesc) Object.defineProperty(globalThis, 'navigator', navigatorDesc)
    else delete globalThis.navigator
    if (documentDesc) Object.defineProperty(globalThis, 'document', documentDesc)
    else delete globalThis.document
  }
})

// No DOM dependency: compile the real TSX and render the function component with a
// minimal JSX runtime. This asserts actual conditional markup for every status.
const require = createRequire(import.meta.url)
const ts = require('typescript')
const componentSource = readFileSync(new URL('../src/TraceDetails.tsx', import.meta.url), 'utf8')
const compiled = ts.transpileModule(componentSource, { compilerOptions: {
  jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
}, reportDiagnostics: true })
const diagnostics = (compiled.diagnostics || []).filter(d => d.category === ts.DiagnosticCategory.Error)
assert.equal(diagnostics.length, 0, ts.formatDiagnosticsWithColorAndContext(diagnostics, {
  getCanonicalFileName: v => v, getCurrentDirectory: () => '.', getNewLine: () => '\n',
}))
const jsx = (type, props) => ({ type, props: props || {} })
const module = { exports: {} }
vm.runInNewContext(compiled.outputText, {
  module, exports: module.exports,
  require: name => name === 'react' ? { useState: x => [x, () => {}], useEffect: () => {} } :
    name === 'react/jsx-runtime' ? { jsx, jsxs: jsx, Fragment: Symbol('Fragment') } :
    name === './traceLinks' ? { grafanaTraceUrl, copyIdentifier } :
    name === './contracts' ? { validTraceId } : (() => { throw new Error(name) })(),
})
const TraceDetails = module.exports.default
function flatten(el) {
  if (el == null || typeof el === 'boolean') return []
  if (Array.isArray(el)) return el.flatMap(flatten)
  if (typeof el !== 'object') return [String(el)]
  return [el, ...flatten(el.props?.children)]
}
const build = (traceId = TRACE, uploadId = ZIP) => flatten(TraceDetails({
  documentId: DOC, traceId, uploadId, grafanaUrl: 'http://127.0.0.1:3101',
}))

test('Component renders ID, copy buttons and safe Grafana link for successful jobs', () => {
  const nodes = build()
  const texts = nodes.filter(item => typeof item === 'string').join(' ')
  assert.match(texts, /Processing trace/)
  assert.match(texts, /Trace ID/)
  assert.match(texts, /Document ID/)
  assert.match(texts, /Upload ID/)
  assert.match(texts, /Copy Trace ID/)
  assert.match(texts, /Copy Run ID/)
  const links = nodes.filter(item => item?.type === 'a')
  assert.equal(links.length, 1)
  assert.equal(links[0].props.rel, 'noopener noreferrer')
  assert.equal(links[0].props.target, '_blank')
  assert.match(links[0].props.href, /\/explore\?/)
})

test('Component keeps document ID while explicitly showing unavailable trace', () => {
  const nodes = build(null, null)
  const text = nodes.filter(item => typeof item === 'string').join(' ')
  assert.match(text, /Trace unavailable/)
  assert.match(text, /Copy Run ID/)
  assert.doesNotMatch(text, /Copy Trace ID/)
  assert.equal(nodes.filter(item => item?.type === 'a').length, 0)
})

test('Component falls back to manual Grafana instructions without public URL', () => {
  const nodes = flatten(TraceDetails({ documentId: DOC, traceId: TRACE, uploadId: null, grafanaUrl: '' }))
  const text = nodes.filter(item => typeof item === 'string').join(' ')
  assert.match(text, /Grafana → Explore → Tempo → Trace ID/)
  assert.equal(nodes.filter(item => item?.type === 'a').length, 0)
})

test('App renders trace controls outside status-specific branches and preserves IDs on poll', () => {
  const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  assert.match(source, /<TraceDetails documentId=\{selected\.id\} traceId=\{selected\.traceId\} uploadId=\{selected\.uploadId\}/)
  assert.match(source, /traceId: found\.doc\.trace_id \?\? job\.traceId/)
  assert.match(source, /uploadId: found\.doc\.upload_id \?\? job\.uploadId/)
  assert.ok(source.indexOf('<TraceDetails') < source.indexOf("{selected.status === 'failed'"))
  assert.match(source, /Do not discard successful status\/Trace ID just because fetching pages failed/)
})


test('Component never displays or copies an invalid trace passed directly', () => {
  const nodes = build('bad-id', ZIP)
  const text = nodes.filter(item => typeof item === 'string').join(' ')
  assert.match(text, /Trace unavailable/)
  assert.doesNotMatch(text, /bad-id/)
  assert.equal(nodes.filter(item => item?.type === 'a').length, 0)
})
