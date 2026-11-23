import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import * as catalogue from '../src/catalogue.ts'
import * as contracts from '../src/contracts.ts'
import * as ui from '../src/ui.ts'
const { routeFor, parseRun, parseLibrary, parseCataloguePage, listUrl, EMPTY_FILTERS, STATUSES } = catalogue
const id='12345678-1234-1234-1234-123456789abc', id2='12345678-1234-1234-1234-123456789abd', hash='a'.repeat(64), date='2026-11-18T12:00:00Z'
const run={run_id:id,document_id:hash,filename:'Math.pdf',parser:'pypdf',status:'completed',cache:'miss',page_count:2,created_at:date,updated_at:date,duration_ms:1000,cost_usd:null,extraction_model:null,summary_model:'test',application_version:'0.12.0a7',has_extraction:true,has_summary:true}
const doc={document_id:hash,sha256:hash,display_name:'Renamed.pdf',filenames:['Math.pdf','Renamed.pdf'],file_size_bytes:300,page_count:2,preferred_run_id:id,created_at:date,updated_at:date,revision:3}
test('default history, independent upload library and system routes, valid durable deep links',()=>{
  assert.deepEqual(routeFor('/'),{view:'history'});assert.equal(routeFor('/upload').view,'upload');assert.equal(routeFor('/library').view,'library');assert.equal(routeFor('/system').view,'system');assert.deepEqual(routeFor(`/runs/${id}`),{view:'run',id});assert.deepEqual(routeFor(`/library/${hash}`),{view:'document',id:hash});for(const bad of ['/runs/../../api','/library/bad','/runs/'+hash,'/unknown'])assert.equal(routeFor(bad).view,'missing')
})
test('all statuses and hit/shared runs are valid independent archive records',()=>{
  for(const status of STATUSES)assert.equal(parseRun({...run,status}).status,status);for(const cache of ['miss','hit','shared'])assert.equal(parseRun({...run,cache}).cache,cache)
  const rows=parseCataloguePage({items:[run,{...run,run_id:id2,filename:'Renamed.pdf',cache:'hit'},run],next_cursor:'opaque'},parseRun,r=>r.run_id)
  assert.equal(rows.items.length,2);assert.equal(rows.items[0].document_id,rows.items[1].document_id);assert.equal(rows.next_cursor,'opaque')
})
test('shared parser guard is a runtime export and accepts every UI parser', () => {
  assert.equal(typeof contracts.isParser, 'function')
  for (const option of ui.PARSERS) assert.equal(contracts.isParser(option.id), true)
  for (const invalid of ['unknown', '', null, [], {}, 12]) assert.equal(contracts.isParser(invalid), false)
})
test('fourth-parser History metadata is accepted alongside legacy parser records', () => {
  const visionRun={...run,parser:'vision_figures',filename:'YOLOv3.pdf',application_version:'0.12.1'}
  const parsed=parseRun(visionRun)
  assert.equal(parsed.parser,'vision_figures')
  const rows=parseCataloguePage({items:[visionRun],next_cursor:null},parseRun,r=>r.run_id)
  assert.equal(rows.items.length,1)
  assert.equal(rows.items[0].parser,'vision_figures')
  for(const parser of ['pypdf','gemini','gpt5_mini','vision_figures']) {
    assert.equal(parseRun({...run,parser}).parser,parser)
  }
  assert.throws(()=>parseRun({...run,parser:'imaginary_parser'}),/Invalid run metadata/)
})
test('all selectable parser options are accepted in History and have a display label', () => {
  for (const option of ui.PARSERS) {
    const parsed = parseRun({...run, parser: option.id})
    assert.equal(parsed.parser, option.id)
    assert.equal(typeof ui.PARSERS.find(p => p.id === parsed.parser)?.name, 'string')
  }
  assert.ok(ui.PARSERS.some(p => p.id === 'vision_figures' && p.name === 'AI OCR + Pictures'))
})
test('unique document identities preserve display name, aliases and preferred run',()=>{assert.deepEqual(parseLibrary(doc,hash).filenames,doc.filenames);assert.equal(parseLibrary(doc).preferred_run_id,id);assert.equal(parseCataloguePage({items:[doc,doc],next_cursor:null},parseLibrary,d=>d.document_id).items.length,1)})
test('malformed or foreign IDs, wrong fields and malformed pages cannot masquerade as valid empty history',()=>{
  for(const bad of [{...run,run_id:'../x'},{...run,document_id:'bad'},{...run,status:'unknown'},{...run,parser:['pypdf']},{...run,cache:'none'},{...run,cost_usd:-1}])assert.throws(()=>parseRun(bad));assert.throws(()=>parseRun(run,id2));assert.throws(()=>parseLibrary({...doc,sha256:'b'.repeat(64)}));assert.throws(()=>parseCataloguePage({items:[],next_cursor:5},parseRun,r=>r.run_id));assert.throws(()=>parseCataloguePage({error:'unavailable'},parseRun,r=>r.run_id));assert.deepEqual(parseCataloguePage({items:[],next_cursor:null},parseRun,r=>r.run_id).items,[])
})
test('filter parameters and opaque cursors are encoded; no raw search DSL',()=>{
  const q=new URL(listUrl('/runs',{...EMPTY_FILTERS,filename:'a & b?*',parser:'pypdf',status:'partial',from:'2026-11-18T00:00',to:'2026-11-18T23:59',sort:'oldest'},'cursor+/= &'),'http://local').searchParams
  assert.equal(q.get('filename'),'a & b?*');assert.equal(q.get('cursor'),'cursor+/= &');assert.equal(q.get('limit'),'50');assert.equal(q.get('sort'),'oldest');assert.equal(q.get('status'),'partial');assert.match(q.get('created_from'),/Z$/);assert.throws(()=>listUrl('/library',{...EMPTY_FILTERS,from:'2026-11-19T00:00',to:'2026-11-18T00:00'},null));assert.equal(new URL(listUrl('/runs',EMPTY_FILTERS,null),'http://local').searchParams.has('cursor'),false)
})
// Execute actual TSX functions with an isolated JSX/hook double. This is not a browser test.
const require=createRequire(import.meta.url), ts=require('typescript')
const source=readFileSync(new URL('../src/Catalogue.tsx',import.meta.url),'utf8'), app=readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8')
const jsx=(type,props)=>({type,props:props||{}}), mod={exports:{}}
const compiled=ts.transpileModule(source,{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},reportDiagnostics:true})
assert.equal(compiled.diagnostics.filter(x=>x.category===ts.DiagnosticCategory.Error).length,0)
vm.runInNewContext(compiled.outputText,{module:mod,exports:mod.exports,require:name=>name==='react'?{useState:x=>[x,()=>{}],useEffect:()=>{}}:name==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:Symbol('fragment')}:name==='./catalogue'?catalogue:name==='./contracts'?contracts:name==='./ui'?ui:(()=>{throw Error(name)})()})
function flatten(v){if(v==null||typeof v==='boolean')return [];if(Array.isArray(v))return v.flatMap(flatten);if(typeof v!=='object')return [v];return [v,...flatten(v.props?.children)]}
test('history and library render distinct persistent listing collections',()=>{const h=flatten(mod.exports.History()),l=flatten(mod.exports.Library());assert.ok(h.includes('Processing History'));assert.ok(l.includes('Document Library'));assert.ok(h.some(n=>n?.props?.path==='/runs'));assert.ok(l.some(n=>n?.props?.path==='/library'));assert.ok(h.some(n=>n?.props?.href==='/upload'))})
test('actual run metadata links only to the canonical document and verified original endpoint',()=>{const nodes=flatten(mod.exports.RunMetadata({run:parseRun(run)}));assert.ok(nodes.some(n=>n?.props?.href===`/library/${hash}`));assert.ok(nodes.some(n=>n?.props?.href===`/api/v1/library/${hash}/original`));assert.ok(nodes.includes('—'))})
test('document detail route and archived result wiring preserve exact downloads and retry overlay',()=>{assert.match(app,/requestDocument\(runId, controller.signal\)/);assert.match(app,/\/api\/v1\/runs\/\$\{selected.id\}\/markdown/);assert.match(app,/\/api\/v1\/runs\/\$\{selected.id\}\/summary/);assert.match(app,/\/retry-summary/);assert.match(source,/\/preferred-run/);assert.match(source,/disabled=\{mutating\|\|r.status!=='completed'\}/);assert.match(source,/Reload document before retrying/);assert.match(source,/controller.abort\(\)/);assert.match(source,/setPage\(result\)/);assert.match(source,/setTrail\(\[null\]\)/)})

test('deletion uses confirmation, explicit HTTP DELETE and revision precondition; no unsafe revision',()=>{
  assert.equal(parseLibrary(doc).revision,3);for(const revision of [0,-1,1.5,Number.MAX_SAFE_INTEGER+1,null])assert.throws(()=>parseLibrary({...doc,revision}))
  assert.match(source,/window.confirm\(question\)/);assert.match(source,/method:'DELETE'/);assert.match(source,/headers\['If-Match'\]/);assert.match(source,/revision=\{doc.revision\}/);assert.match(source,/Delete document and all runs/);assert.match(source,/\['completed','partial','failed'\].includes\(run.status\)/)
})
