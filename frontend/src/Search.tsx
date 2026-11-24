import { useEffect, useState, type FormEvent } from 'react'
import { PARSERS } from './ui'
import { parseMatches, parseSearch, highlightedSegments, type FoundDocument, type Match, type MatchResponse, type SearchOptions, type SearchResponse } from './search'

const DEFAULTS: SearchOptions = { query:'', match:'all', scope:'preferred', parser:null, sort:'relevance', offset:0, limit:10 }

async function postSearch(endpoint: string, options: SearchOptions, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(endpoint, { method:'POST', cache:'no-store', signal,
    headers:{ 'Content-Type':'application/json' }, body:JSON.stringify(options) })
  const value: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const payload = value && typeof value === 'object' ? value as {error?: {message?: string}} : null
    throw new Error(payload?.error?.message || `Search returned HTTP ${response.status}`)
  }
  return value
}

function Snippet({text}: {text:string}) {
  return <span>{highlightedSegments(text).map((part, i) => part.highlighted
    ? <mark key={i}>{part.text}</mark> : <span key={i}>{part.text}</span>)}</span>
}
function MatchList({items}: {items:Match[]}) {
  return <ul className="search-matches">{items.map(item => <li key={`${item.run_id}-${item.page}-${item.passage_index}`}>
    <div className="search-match-meta"><span>Page {item.page}</span><span>{PARSERS.find(p=>p.id===item.parser)?.name ?? item.parser}</span>
      <a href={`/runs/${item.run_id}?page=${item.page}`}>Open page →</a></div>
    <div className="search-snippet"><Snippet text={item.snippet}/></div>
  </li>)}</ul>
}
function ResultCard({doc, options}: {doc: FoundDocument; options: SearchOptions}) {
  const [expanded, setExpanded] = useState<MatchResponse|null>(null)
  const [error,setError] = useState<string|null>(null)
  const [busy,setBusy] = useState(false)
  const [visible,setVisible] = useState(false)
  async function load(offset: number) {
    if (busy) return
    setBusy(true); setError(null)
    try {
      const result = parseMatches(await postSearch(`/api/v1/search/${doc.document_id}/matches`, {...options,offset,limit:20}))
      setExpanded(current => offset && current ? {...result,items:[...current.items,...result.items]} : result)
      setVisible(true)
    } catch(e) { setError(e instanceof Error? e.message : 'Could not load matches.') }
    finally {setBusy(false)}
  }
  return <article className="search-result">
    <div className="search-result-heading"><div>
      <h2><a href={`/library/${doc.document_id}`}>{doc.filename}</a></h2>
      <p>{doc.matching_passages} matching {doc.matching_passages===1?'passage':'passages'} · PDF grouped by original SHA-256</p>
    </div><a className="secondary-button" href={`/library/${doc.document_id}`}>Document details</a></div>
    <MatchList items={visible && expanded ? expanded.items : doc.passages}/>
    {doc.matching_passages > doc.passages.length && !visible && <button className="secondary-button" onClick={()=>void load(0)} disabled={busy}>Show matching passages</button>}
    {visible && expanded?.has_more && <button className="secondary-button" onClick={()=>void load(expanded.items.length)} disabled={busy}>{busy?'Loading…':'Load more matches'}</button>}
    {visible && <button className="secondary-button" onClick={()=>setVisible(false)}>Show top matches</button>}
    {error && <p role="alert" className="notice error">{error}</p>}
  </article>
}

export default function Search() {
  const [draft,setDraft] = useState<SearchOptions>({...DEFAULTS})
  const [active,setActive] = useState<SearchOptions|null>(null)
  const [result,setResult] = useState<SearchResponse|null>(null)
  const [error,setError] = useState<string|null>(null)
  const [busy,setBusy] = useState(false)
  const [requestId,setRequestId] = useState(0)
  useEffect(()=>{
    if (!active) return
    const controller=new AbortController()
    setBusy(true);setError(null);setResult(null)
    void postSearch('/api/v1/search',active,controller.signal).then(raw=>{
      if (!controller.signal.aborted) setResult(parseSearch(raw))
    }).catch(e=>{if(!controller.signal.aborted)setError(e instanceof Error?e.message:'Search failed.')})
      .finally(()=>{if(!controller.signal.aborted)setBusy(false)})
    return ()=>controller.abort()
  },[active,requestId])
  function submit(e:FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (draft.query.trim().length < 2 || busy) return
    setActive({...draft,query:draft.query.trim(),offset:0});setRequestId(id=>id+1)
  }
  return <main className="container main-content catalogue-main search-page">
    <div className="catalogue-heading"><div><p className="panel-overline">PERMANENT WORKSPACE</p><h1>Full-text Search</h1>
      <p>Search extracted PDF text across archived documents. Results are ranked by matching passages and grouped by original PDF.</p></div></div>
    <section className="panel catalogue-panel"><form className="search-form" onSubmit={submit}>
      <label htmlFor="search-terms">Search words or exact phrase</label>
      <div className="search-input-row"><input id="search-terms" type="search" value={draft.query} minLength={2} maxLength={200} placeholder="Search extracted documents…" onChange={e=>setDraft({...draft,query:e.target.value})}/>
        <button className="catalogue-primary" type="submit" disabled={busy||draft.query.trim().length<2}>Search</button></div>
      <div className="search-filters">
        <label>Match<select value={draft.match} onChange={e=>setDraft({...draft,match:e.target.value as SearchOptions['match']})}><option value="all">All terms</option><option value="any">Any term</option><option value="phrase">Exact phrase</option></select></label>
        <label>Extraction scope<select value={draft.scope} onChange={e=>setDraft({...draft,scope:e.target.value as SearchOptions['scope']})}><option value="preferred">Preferred extraction</option><option value="all">All extractions</option></select></label>
        <label>Parser<select value={draft.parser??''} onChange={e=>setDraft({...draft,parser:e.target.value||null})}><option value="">All parsers</option>{PARSERS.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>Sort<select value={draft.sort} onChange={e=>setDraft({...draft,sort:e.target.value as SearchOptions['sort']})}><option value="relevance">Best match</option><option value="newest">Newest extraction</option></select></label>
      </div>
    </form></section>
    {error&&<p className="notice error" role="alert">{error} Check OpenSearch and run the offline passage-index rebuild if needed.</p>}
    {busy&&<p role="status" className="catalogue-empty">Searching indexed text…</p>}
    {!busy&&active&&result&&<section aria-label="Search results" className="search-results">
      <p className="catalogue-muted">{result.items.length ? `Showing ${result.items.length} unique PDFs on this page` : 'No matching documents'} · Scores are relative, not percentages. No AI requests are made.</p>
      {result.items.map(doc=><ResultCard key={`${doc.document_id}-${requestId}`} doc={doc} options={active}/>)}
      <div className="catalogue-pagination"><button className="secondary-button" type="button" disabled={active.offset===0} onClick={()=>setActive({...active,offset:Math.max(0,active.offset-active.limit)})}>Previous</button>
        <button className="secondary-button" type="button" disabled={!result.has_more||active.offset+active.limit>2000} onClick={()=>setActive({...active,offset:active.offset+active.limit})}>Next</button></div>
    </section>}
    {!active&&<p className="catalogue-muted">Existing documents can be indexed with <code>python -m app.search_cli rebuild</code> without reprocessing PDFs.</p>}
  </main>
}
