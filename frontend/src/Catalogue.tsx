import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { apiError } from './contracts'
import { PARSERS, STATUS_LABELS, formatBytes } from './ui'
import { EMPTY_FILTERS, STATUSES, listUrl, localDate, parseCataloguePage, parseLibrary, parseRun, type CataloguePage, type Filters, type RunRecord, type LibraryRecord } from './catalogue'

export async function catalogueJson(url: string, init: RequestInit = {}): Promise<unknown> {
  const response = await fetch(url, { cache: 'no-store', ...init })
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(apiError(response.status, data))
  return data
}
const message = (e: unknown) => e instanceof Error ? e.message : 'Request failed.'
export function DeleteControl({ path, label, question, target, disabled=false, revision }: {path:string;label:string;question:string;target:string;disabled?:boolean;revision?:number}) {
  const [busy,setBusy]=useState(false), [error,setError]=useState<string|null>(null)
  async function remove() {
    if (busy || disabled || !window.confirm(question)) return
    setBusy(true);setError(null)
    try {
      const headers:Record<string,string>={}
      if (revision!==undefined) headers['If-Match']=`"${revision}"`
      await catalogueJson(path,{method:'DELETE',headers})
      window.location.assign(target)
    } catch(e) {
      setError(`${message(e)} Refresh before retrying; a deletion may be awaiting reconciliation.`)
      setBusy(false)
    }
  }
  return <span className="catalogue-delete"><button className="secondary-button catalogue-danger" type="button" onClick={()=>void remove()} disabled={busy||disabled}>{busy?'Deleting…':label}</button>{error&&<span className="notice error" role="alert">{error}</span>}</span>
}
export function RunMetadata({ run }: { run: RunRecord }) {
  return <div className="catalogue-run-meta"><div className="catalogue-actions">
    <a className="secondary-button" href={`/library/${run.document_id}`}>View in library</a>
    <a className="secondary-button" href={`/api/v1/library/${run.document_id}/original`}>Original PDF</a>
    <span className="catalogue-muted">Submitted {localDate(run.created_at)}</span><DeleteControl path={`/api/v1/runs/${run.run_id}`} label="Delete run" question="Permanently delete this processing run and its extracted results? The original PDF and other runs will remain." target="/" disabled={!['completed','partial','failed'].includes(run.status)}/></div>
    <dl className="catalogue-facts"><div><dt>Document SHA-256</dt><dd className="catalogue-id">{run.document_id}</dd></div><div><dt>Extraction model</dt><dd>{run.extraction_model ?? '—'}</dd></div><div><dt>Summary model</dt><dd>{run.summary_model ?? '—'}</dd></div><div><dt>Duration</dt><dd>{run.duration_ms == null ? '—' : `${(run.duration_ms / 1000).toFixed(1)} s`}</dd></div><div><dt>Cost</dt><dd>{run.cost_usd == null ? '—' : `$${run.cost_usd.toFixed(4)}`}</dd></div><div><dt>Application version</dt><dd>{run.application_version}</dd></div></dl>
  </div>
}
function FilterForm({ onApply }: { onApply: (filters: Filters) => void }) {
  const [draft, setDraft] = useState<Filters>({...EMPTY_FILTERS})
  const [error, setError] = useState<string | null>(null)
  function submit(e: FormEvent) {
    e.preventDefault()
    try { listUrl('/runs', draft, null); setError(null); onApply(draft) } catch (err) { setError(message(err)) }
  }
  return <form className="catalogue-filters" onSubmit={submit}>
    <label>Filename<input value={draft.filename} maxLength={200} placeholder="Search filenames" onChange={e => setDraft({...draft, filename:e.target.value})}/></label>
    <label>Parser<select value={draft.parser} onChange={e => setDraft({...draft,parser:e.target.value})}><option value="">All parsers</option>{PARSERS.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
    <label>Status<select value={draft.status} onChange={e => setDraft({...draft,status:e.target.value})}><option value="">All statuses</option>{STATUSES.map(s => <option key={s} value={s}>{STATUS_LABELS[s]}</option>)}</select></label>
    <label>Created from<input type="datetime-local" value={draft.from} onChange={e => setDraft({...draft,from:e.target.value})}/></label>
    <label>Created to<input type="datetime-local" value={draft.to} onChange={e => setDraft({...draft,to:e.target.value})}/></label>
    <label>Sort<select value={draft.sort} onChange={e => setDraft({...draft,sort:e.target.value as Filters['sort']})}><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label>
    <div className="catalogue-actions"><button type="submit" className="secondary-button">Apply filters</button><button type="button" className="secondary-button" onClick={() => {setDraft({...EMPTY_FILTERS});setError(null);onApply({...EMPTY_FILTERS})}}>Clear</button></div>
    {error && <p className="notice error" role="alert">{error}</p>}
  </form>
}
function Listing<T>({ path, parse, identity, render, refreshKey=0, empty }: { path:string; parse:(v:unknown)=>T; identity:(v:T)=>string; render:(items:T[])=>ReactNode; refreshKey?:number; empty:string }) {
  const [filters,setFilters] = useState<Filters>({...EMPTY_FILTERS})
  const [trail,setTrail] = useState<(string|null)[]>([null])
  const [page,setPage] = useState<CataloguePage<T>|null>(null)
  const [error,setError] = useState<string|null>(null)
  const [busy,setBusy] = useState(true)
  const [refresh,setRefresh] = useState(0)
  const cursor=trail[trail.length-1]
  const url=listUrl(path,filters,cursor)
  // Replace each page, never append polling results. Abort obsolete filters/navigation.
  useEffect(() => {
    const controller=new AbortController(); let timer:number|undefined
    setPage(null);setBusy(true);setError(null)
    async function load() {
      try {
        const raw=await catalogueJson(url,{signal:controller.signal})
        const result=parseCataloguePage(raw,parse,identity)
        if (!controller.signal.aborted) {setPage(result);setError(null)}
      } catch(e) {if(!controller.signal.aborted) setError(message(e))}
      finally {
        if(!controller.signal.aborted) {setBusy(false); if(cursor===null) timer=window.setTimeout(load,5000)}
      }
    }
    void load()
    return () => {controller.abort();window.clearTimeout(timer)}
  },[url,refresh,refreshKey,cursor,parse,identity])
  function apply(next:Filters) {setPage(null);setBusy(true);setError(null);setTrail([null]);setFilters(next);setRefresh(n=>n+1)}
  return <>
    <FilterForm onApply={apply}/>
    <div className="catalogue-list-toolbar"><span className="catalogue-muted">Page {trail.length}{page ? ` · ${page.items.length} shown` : ''} · {cursor===null?'Auto-refresh every 5 seconds':'Refresh manually for older pages'}</span><button className="secondary-button" type="button" onClick={()=>{setTrail([null]);setRefresh(n=>n+1)}}>Refresh from first page</button></div>
    {error && <p className="notice error" role="alert">{error}{page?' — showing last successful page; data may be stale.':''}</p>}
    {busy ? <p className="catalogue-empty" role="status">Loading…</p> : page ? page.items.length ? render(page.items) : !error && <p className="catalogue-empty">{empty}</p> : !error && <p className="catalogue-empty">No response yet.</p>}
    <div className="catalogue-pagination"><button className="secondary-button" type="button" disabled={busy||trail.length===1} onClick={()=>{setPage(null);setBusy(true);setTrail(t=>t.slice(0,-1))}}>Previous page</button><button className="secondary-button" type="button" disabled={busy||!!error||!page?.next_cursor||trail.includes(page.next_cursor)} onClick={()=>{if(page?.next_cursor){setPage(null);setBusy(true);setTrail(t=>[...t,page.next_cursor])}}}>Next page</button></div>
  </>
}
const runKey=(r:RunRecord)=>r.run_id
const docKey=(d:LibraryRecord)=>d.document_id
function RunsTable({ items, preferred, onPrefer, mutating }: {items:RunRecord[];preferred?:string|null;onPrefer?:(id:string)=>void;mutating?:boolean}) {
  return <div className="catalogue-table-wrap"><table className="catalogue-table"><thead><tr><th>Submitted filename</th><th>Parser</th><th>Status</th><th>Cache</th><th>Submitted</th><th>Pages</th>{onPrefer&&<th>Preferred</th>}<th>Delete</th></tr></thead><tbody>{items.map(r=><tr key={r.run_id}><td><a href={`/runs/${r.run_id}`}>{r.filename}</a><small className="catalogue-muted catalogue-id">{r.run_id}</small></td><td>{PARSERS.find(p=>p.id===r.parser)?.name}</td><td><span className={`status-pill ${r.status}`}><span className="status-dot"/>{STATUS_LABELS[r.status]}</span></td><td>{r.cache==='miss'?'New':r.cache==='hit'?'Hit':'Shared'}</td><td>{localDate(r.created_at)}</td><td>{r.page_count??'—'}</td>{onPrefer&&<td>{preferred===r.run_id?<span className="preferred-label">Preferred</span>:<button className="secondary-button" type="button" disabled={mutating||r.status!=='completed'} onClick={()=>onPrefer(r.run_id)}>Set preferred</button>}</td>}<td><DeleteControl path={`/api/v1/runs/${r.run_id}`} label="Delete" question="Permanently delete this processing run? Other runs and the original PDF will remain." target={onPrefer?`/library/${r.document_id}`:'/'} disabled={!!mutating||!['completed','partial','failed'].includes(r.status)}/></td></tr>)}</tbody></table></div>
}
export function History() {
  return <main className="container main-content catalogue-main"><div className="catalogue-heading"><div><p className="panel-overline">PERMANENT WORKSPACE</p><h1>Processing History</h1><p>Every submitted request, including cached, shared, active and failed runs.</p></div><a className="catalogue-primary" href="/upload">+ New processing job</a></div><section className="panel catalogue-panel"><Listing path="/runs" parse={parseRun} identity={runKey} empty="No processing requests match these filters. Upload a PDF or clear the filters." render={items=><RunsTable items={items}/>}/></section></main>
}
export function Library() {
  return <main className="container main-content catalogue-main"><div className="catalogue-heading"><div><p className="panel-overline">CANONICAL ORIGINALS</p><h1>Document Library</h1><p>Unique PDFs grouped by SHA-256, with all their processing runs.</p></div><a className="catalogue-primary" href="/upload">+ New processing job</a></div><section className="panel catalogue-panel"><Listing path="/library" parse={parseLibrary} identity={docKey} empty="No documents match these filters." render={items=><div className="catalogue-table-wrap"><table className="catalogue-table"><thead><tr><th>Document</th><th>Size</th><th>Pages</th><th>Created</th><th>Preferred result</th></tr></thead><tbody>{items.map(d=><tr key={d.document_id}><td><a href={`/library/${d.document_id}`}>{d.display_name}</a><small className="catalogue-muted catalogue-id">{d.document_id}</small></td><td>{formatBytes(d.file_size_bytes)}</td><td>{d.page_count??'—'}</td><td>{localDate(d.created_at)}</td><td>{d.preferred_run_id?<a href={`/runs/${d.preferred_run_id}`}>Open result</a>:'—'}</td></tr>)}</tbody></table></div>}/></section></main>
}
export function LibraryDetail({ id }: {id:string}) {
  const [doc,setDoc]=useState<LibraryRecord|null>(null), [name,setName]=useState(''), [error,setError]=useState<string|null>(null), [busy,setBusy]=useState(false), [refresh,setRefresh]=useState(0)
  const path=`/api/v1/library/${id}`
  useEffect(()=>{const controller=new AbortController();setError(null);setDoc(null);setBusy(true);void catalogueJson(path,{signal:controller.signal}).then(raw=>{const d=parseLibrary(raw,id);if(!controller.signal.aborted){setDoc(d);setName(d.display_name)}}).catch(e=>{if(!controller.signal.aborted)setError(message(e))}).finally(()=>{if(!controller.signal.aborted)setBusy(false)});return()=>controller.abort()},[path,id,refresh])
  async function mutate(suffix:string,body:object) {
    if(busy)return;setBusy(true);setError(null)
    try {const d=parseLibrary(await catalogueJson(path+suffix,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),id);setDoc(d);setName(d.display_name)}
    catch(e){setError(`${message(e)} Reload document before retrying; the archive change may already have committed.`)}
    finally{setBusy(false)}
  }
  return <main className="container main-content catalogue-main"><a className="catalogue-back" href="/library">← Document Library</a><div className="catalogue-heading"><div><p className="panel-overline">DOCUMENT DETAILS</p><h1>{doc?.display_name??'Archived document'}</h1></div><button type="button" className="secondary-button" disabled={busy} onClick={()=>setRefresh(n=>n+1)}>Reload document</button></div>{error&&<p className="notice error" role="alert">{error}</p>}{!doc?(busy&&<p role="status" className="catalogue-empty">Loading document…</p>):<><section className="panel catalogue-panel"><div className="catalogue-actions"><a className="catalogue-primary" href={`${path}/original`}>Download original PDF</a>{doc.preferred_run_id&&<a className="secondary-button" href={`/runs/${doc.preferred_run_id}`}>Open preferred result</a>}</div><dl className="catalogue-facts"><div><dt>SHA-256</dt><dd className="catalogue-id">{doc.document_id}</dd></div><div><dt>Size</dt><dd>{formatBytes(doc.file_size_bytes)}</dd></div><div><dt>Pages</dt><dd>{doc.page_count??'—'}</dd></div><div><dt>Created</dt><dd>{localDate(doc.created_at)}</dd></div><div><dt>Known filenames</dt><dd>{doc.filenames.join(' · ')}</dd></div></dl><form className="catalogue-rename" onSubmit={e=>{e.preventDefault();void mutate('',{display_name:name.trim()})}}><label>Display name<input value={name} onChange={e=>setName(e.target.value)} required maxLength={255} disabled={busy||!!error}/></label><button className="secondary-button" type="submit" disabled={busy||!!error||!name.trim()||name.trim()===doc.display_name}>Save name</button></form><p className="catalogue-muted">Renaming does not change the original PDF or the submitted names of historical runs.</p><div className="catalogue-actions"><DeleteControl path={path} label="Delete document and all runs" question="Permanently delete this original PDF, every processing run and all associated results? This cannot be undone." revision={doc.revision} target="/library" disabled={busy||!!error}/></div><p className="catalogue-muted">Deletion requires an idle processing queue. Active runs cannot be deleted.</p></section><section className="panel catalogue-panel"><h2>Processing runs</h2><p className="catalogue-muted">Only completed runs can be preferred.</p><Listing path={`/library/${id}/runs`} parse={parseRun} identity={runKey} refreshKey={refresh} empty="No runs match these filters." render={items=><RunsTable items={items} preferred={doc.preferred_run_id} mutating={busy||!!error} onPrefer={runId=>void mutate('/preferred-run',{run_id:runId})}/>}/></section></>}</main>
}
