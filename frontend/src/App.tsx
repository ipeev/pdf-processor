import { useEffect, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import { version as appVersion } from '../package.json'
import SystemMonitor from './SystemMonitor'
import Search from './Search'
import { History, Library, LibraryDetail, RunMetadata, catalogueJson } from './Catalogue'
import { parseRun, routeFor, type RunRecord } from './catalogue'
import { isActiveRun, onlyActiveRuns, readActiveUploadIds, writeActiveUploadIds } from './activeUploads'
import TraceDetails from './TraceDetails'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import { normalizeDisplayMath } from './math'
import {
  apiError, diagnosticEntries, formatProcessingError, parseDocumentStatus, parsePages, parseUploadResponse, validateFiles, FILE_LIMITS,
  type CacheState, type DocumentStatus, type Page, type Parser, type Status, type ProviderDiagnostic, type PageProgress,
} from './contracts'
import { PARSERS, STATUS_LABELS, processingSteps, formatBytes, safeMarkdownUrl } from './ui'

type Job = {
  id: string
  filename: string
  parser: Parser
  status: Status
  cache: CacheState
  summary: string | null
  pageCount: number | null
  pageProgress: PageProgress | null
  pages: Page[] | null
  error: string | null
  diagnostic: ProviderDiagnostic | null
  traceId: string | null
  uploadId: string | null
}

type ResultTab = 'summary' | 'pages'
type IconName = 'file' | 'upload' | 'arrow' | 'layers' | 'spark' | 'scan' | 'check' | 'clock' |
  'warning' | 'close' | 'download' | 'chevron' | 'repeat' | 'folder' | 'bolt'

function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  const shapes: Record<IconName, ReactNode> = {
    file: <><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M13 2v7h7M8 13h8M8 17h6"/></>,
    upload: <><path d="M12 16V4m-5 5 5-5 5 5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></>,
    arrow: <><path d="M5 12h14m-6-6 6 6-6 6"/></>,
    layers: <><path d="m12 2 9 5-9 5-9-5 9-5Zm-9 10 9 5 9-5M3 17l9 5 9-5"/></>,
    spark: <><path d="m12 3 1.8 6.2L20 11l-6.2 1.8L12 19l-1.8-6.2L4 11l6.2-1.8L12 3ZM19 18l.7 2.3L22 21l-2.3.7L19 24l-.7-2.3L16 21l2.3-.7L19 18Z"/></>,
    scan: <><path d="M7 3H5a2 2 0 0 0-2 2v2m14-4h2a2 2 0 0 1 2 2v2M3 17v2a2 2 0 0 0 2 2h2m14-4v2a2 2 0 0 1-2 2h-2M3 12h18"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    warning: <><path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3v.01"/></>,
    close: <path d="M6 6 18 18M18 6 6 18"/>,
    download: <><path d="M12 3v13m-5-5 5 5 5-5M4 17v3h16v-3"/></>,
    chevron: <path d="m9 6 6 6-6 6"/>,
    repeat: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.2 9a7 7 0 0 1 12-2L20 12M4 12l2.8 5a7 7 0 0 0 12-2"/></>,
    folder: <><path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z"/></>,
    bolt: <path d="m13 2-9 12h7l-1 8 10-12h-7V2Z"/>,
  }
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{shapes[name]}</svg>
}

// Markdown comes from untrusted PDFs. No raw HTML is rendered. Only verified
// run-owned PNG references may render as images; all others remain placeholders.
function safeUrl(url: string): string {
  return safeMarkdownUrl(url, window.location.origin)
}

async function getJson(response: Response): Promise<unknown> {
  try { return await response.json() } catch { return null }
}

async function requestDocument(id: string, signal?: AbortSignal): Promise<DocumentStatus> {
  const response = await fetch(`/api/v1/documents/${encodeURIComponent(id)}`, { cache: 'no-store', signal })
  const data = await getJson(response)
  if (!response.ok) throw new Error(apiError(response.status, data))
  return parseDocumentStatus(data, id)
}

async function requestPages(id: string, count: number | null, signal?: AbortSignal): Promise<Page[]> {
  const response = await fetch(`/api/v1/documents/${encodeURIComponent(id)}/pages`, { cache: 'no-store', signal })
  const data = await getJson(response)
  if (!response.ok) throw new Error(apiError(response.status, data))
  return parsePages(data, id, count)
}

function Markdown({ text, imageRunId }: { text: string; imageRunId?: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]}
    rehypePlugins={[[rehypeKatex, { trust: false, throwOnError: false, maxExpand: 1000, maxSize: 10 }]]}
    urlTransform={safeUrl} components={{
    img: ({ alt, src }) => {
      const filename = typeof src === 'string' && /^images\/p[0-9]{4}-figure-[A-Za-z0-9_-]{1,70}\.png$/.test(src)
        ? src.slice('images/'.length) : null
      return filename && imageRunId && /^[0-9a-f-]{36}$/.test(imageRunId)
        ? <img loading="lazy" src={`/api/v1/runs/${imageRunId}/images/${filename}`} alt={alt || 'PDF figure'}/>
        : <span>[Image: {alt ?? ''}]</span>
    },
    a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
  }}>{normalizeDisplayMath(text)}</ReactMarkdown></div>
}

function StatusPill({ status }: { status: Status }) {
  return <span className={`status-pill ${status}`}><span className="status-dot" />{STATUS_LABELS[status]}</span>
}

function FileDropzone({ files, disabled, onFiles }: { files: File[]; disabled: boolean; onFiles: (files: File[]) => void }) {
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  function receive(selected: File[]) {
    onFiles(selected)
    if (inputRef.current) inputRef.current.value = ''
  }
  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    if (!disabled) receive(Array.from(event.dataTransfer.files))
  }
  function onDragOver(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    if (!disabled) setDragging(true)
  }
  function onDragLeave(event: DragEvent<HTMLDivElement>) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false)
  }

  return <>
    <div className={`dropzone ${dragging ? 'is-dragging' : ''} ${disabled ? 'is-disabled' : ''}`}
      onDrop={onDrop} onDragOver={onDragOver} onDragLeave={onDragLeave}>
      <span className="upload-icon"><Icon name="upload" size={27}/></span>
      <p className="drop-title">Drop PDF files or a ZIP here</p>
      <p className="drop-subtitle">or choose PDFs or one ZIP archive from your device</p>
      <input ref={inputRef} className="sr-only" aria-label="Select PDFs or ZIP archive" id="pdf-files" type="file"
        accept=".pdf,.zip,application/pdf,application/zip" multiple onChange={(event: ChangeEvent<HTMLInputElement>) => receive(Array.from(event.target.files ?? []))} disabled={disabled}/>
      <button className="browse-button" type="button" onClick={() => inputRef.current?.click()} disabled={disabled}>
        Browse files <Icon name="arrow" size={16}/>
      </button>
      <p className="drop-limits">Up to {FILE_LIMITS.maxFiles} PDFs, or one ZIP with up to {FILE_LIMITS.maxZipPdfs} PDFs · 20 MiB per PDF · 50 MiB total</p>
    </div>
    {files.length > 0 && <div className="selected-files" aria-label="Selected files">
      <div className="selected-heading"><span>Ready to upload</span><span>{files.length} / {FILE_LIMITS.maxFiles}</span></div>
      {files.map((file, i) => <div className="selected-file" key={`${file.name}-${file.size}-${i}`}>
        <span className="tiny-file-icon"><Icon name="file" size={16}/></span>
        <span className="selected-file-name" title={file.name}>{file.name}<small>{formatBytes(file.size)}</small></span>
        <button className="icon-button" type="button" aria-label={`Remove ${file.name}`} disabled={disabled}
          onClick={() => onFiles(files.filter((_, index) => index !== i))}><Icon name="close" size={17}/></button>
      </div>)}
    </div>}
  </>
}

function Workspace({ runId }: { runId?: string }) {
  const [files, setFiles] = useState<File[]>([])
  const [parser, setParser] = useState<Parser>('pypdf')
  const [jobs, setJobs] = useState<Job[]>([])
  const [restoringUploads, setRestoringUploads] = useState(!runId)
  // The Upload workspace stays mounted while navigating. This additional registry
  // reattaches active runs after a full browser reload without storing PDF data.
  useEffect(() => {
    if (runId) return
    const controller = new AbortController()
    const storage = (() => { try { return window.sessionStorage } catch { return null } })()
    const ids = storage ? readActiveUploadIds(storage) : []
    if (!ids.length) { setRestoringUploads(false); return () => controller.abort() }
    void Promise.all(ids.map(async id => {
      try { return await requestDocument(id, controller.signal) }
      catch { return null }
    })).then(results => {
      if (controller.signal.aborted) return
      const restored = results.flatMap((doc, index): Job[] => doc && isActiveRun(doc.status) ? [{
        id: ids[index], filename: doc.filename, parser: doc.parser, status: doc.status, cache: doc.cache,
        summary: doc.summary, pageCount: doc.page_count, pageProgress: doc.page_progress, pages: null,
        error: null, diagnostic: null, traceId: doc.trace_id, uploadId: doc.upload_id,
      }] : [])
      // Do not overwrite uploads accepted while the restore request was in flight.
      setJobs(previous => onlyActiveRuns([...previous, ...restored.filter(item => !previous.some(job => job.id === item.id))]))
      setRestoringUploads(false)
    })
    return () => controller.abort()
  }, [runId])
  useEffect(() => {
    if (!runId && !restoringUploads) {
      try { writeActiveUploadIds(window.sessionStorage, jobs.map(job => job.id)) } catch { /* Storage unavailable. */ }
    }
  }, [jobs, runId, restoringUploads])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tab, setTab] = useState<ResultTab>('summary')
  const [pageIndex, setPageIndex] = useState(() => {
    const number = Number(new URLSearchParams(window.location.search).get('page'))
    return runId && Number.isSafeInteger(number) && number >= 1 && number <= 100000 ? number - 1 : 0
  })
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [pollError, setPollError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState<string | null>(null)
  const [opening, setOpening] = useState(Boolean(runId))
  const [reload, setReload] = useState(0)
  const [runMeta, setRunMeta] = useState<RunRecord | null>(null)
  const [metaError, setMetaError] = useState<string | null>(null)
  // A direct run URL is independent of session state, OpenSearch list availability and browser storage.
  useEffect(() => {
    if (!runId) return
    const controller = new AbortController()
    setOpening(true); setPollError(null); setJobs([]); setSelectedId(null); setRunMeta(null); setMetaError(null)
    void requestDocument(runId, controller.signal).then(doc => {
      if (controller.signal.aborted) return
      setJobs([{ id: runId, filename: doc.filename, parser: doc.parser, status: doc.status, cache: doc.cache,
        summary: doc.summary, pageCount: doc.page_count, pageProgress: doc.page_progress, pages: null,
        error: doc.error ? formatProcessingError(doc.error) : null, diagnostic: doc.error?.diagnostic ?? null,
        traceId: doc.trace_id, uploadId: doc.upload_id }])
      setSelectedId(runId)
    }).catch(error => { if (!controller.signal.aborted) setPollError(error instanceof Error ? error.message : 'Run unavailable.') })
      .finally(() => { if (!controller.signal.aborted) setOpening(false) })
    return () => controller.abort()
  }, [runId, reload])

  function selectFiles(nextFiles: File[]) {
    setFiles(nextFiles)
    setUploadError(nextFiles.length ? validateFiles(nextFiles) : null)
  }

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (uploading) return
    const validationError = validateFiles(files)
    if (validationError) { setUploadError(validationError); return }
    setUploading(true)
    setUploadError(null)
    try {
      const body = new FormData()
      for (const file of files) body.append('files', file)
      body.append('parser', parser)
      // Never automatically retry a POST; Redis may have accepted it even if the response is lost.
      const response = await fetch('/api/v1/documents', { method: 'POST', body })
      const data = await getJson(response)
      if (response.status !== 202) throw new Error(apiError(response.status, data))
      const zipUpload = files.length === 1 && files[0].name.toLowerCase().endsWith('.zip')
      const accepted = parseUploadResponse(data, zipUpload ? { min: 1, max: FILE_LIMITS.maxZipPdfs } : files.length)
      setJobs(previous => onlyActiveRuns([
        ...accepted.map(doc => ({
          id: doc.id, filename: doc.filename, parser: doc.parser, status: doc.status, cache: doc.cache,
          summary: null, pageCount: null, pageProgress: null, pages: null, error: null, diagnostic: null,
          traceId: null, uploadId: null,
        })),
        ...previous,
      ]))
      const firstActive = accepted.find(doc => isActiveRun(doc.status))
      if (firstActive) {
        setSelectedId(firstActive.id)
        setTab('summary')
        setPageIndex(0)
      }
      setFiles([])
    } catch (error) {
      // A failed connection or malformed HTTP 202 response can leave an uncertain
      // enqueue outcome; never auto-submit the same files a second time.
      const message = error instanceof Error ? error.message : 'Upload failed.'
      const uncertain = (error instanceof TypeError) || message.includes('unexpected upload response') || message.includes('invalid document ID')
      setUploadError(uncertain ? `${message} The upload may have been accepted. Do not resubmit automatically.` : message)
    } finally {
      setUploading(false)
    }
  }

  async function retrySummary(id: string) {
    if (retrying) return
    setRetrying(id)
    try {
      const response = await fetch(`/api/v1/documents/${encodeURIComponent(id)}/retry-summary`, { method: 'POST' })
      const data = await getJson(response)
      if (response.status !== 202) throw new Error(apiError(response.status, data))
      setJobs(previous => previous.map(job => job.id === id ? {
        ...job, status: 'summarizing' as Status, pageProgress: null, error: null, diagnostic: null, summary: null,
      } : job))
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Retry request failed.'
      setPollError(`Could not retry the summary: ${message}`)
    } finally {
      setRetrying(null)
    }
  }

  // Poll only unfinished jobs (or completed jobs whose pages have not loaded).
  // Re-scheduling from state makes polling stop when everything reaches a terminal state.
  // Durable history is queried separately; only this open result or current upload batch is polled here.
  useEffect(() => {
    const pending = jobs.filter(job => (['queued', 'processing', 'summarizing'].includes(job.status)) ||
      ((job.status === 'completed' || job.status === 'partial') && job.pages === null))
    if (opening || pending.length === 0) return
    let cancelled = false
    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      const results = await Promise.all(pending.map(async job => {
        try {
          const doc = await requestDocument(job.id, controller.signal)
          try {
            const pages = ((runId || isActiveRun(doc.status)) && ['completed', 'partial', 'summarizing'].includes(doc.status) && doc.page_count) ? await requestPages(job.id, doc.page_count, controller.signal) : null
            return { id: job.id, doc, pages, error: null }
          } catch (error) {
            // Do not discard successful status/Trace ID just because fetching pages failed.
            return { id: job.id, doc, pages: null, error: error instanceof Error ? error.message : 'Pages unavailable' }
          }
        } catch (error) {
          return { id: job.id, doc: null, pages: null, error: error instanceof Error ? error.message : 'Network error' }
        }
      }))
      if (cancelled) return
      const firstError = results.find(item => item.error)?.error ?? null
      setPollError(firstError ? `Some results could not be refreshed. Retrying: ${firstError}` : null)
      setJobs(previous => {
        const updated = previous.map(job => {
          const found = results.find(item => item.id === job.id)
          if (!found?.doc) return job
          return {
          ...job,
          status: found.doc.status,
          cache: found.doc.cache,
          summary: found.doc.summary,
          pageCount: found.doc.page_count,
          pageProgress: found.doc.page_progress,
          pages: found.pages ?? job.pages,
          error: (found.doc.status === 'failed' || found.doc.status === 'partial') ?
            (found.doc.error ? formatProcessingError(found.doc.error) : 'Processing failed.') : null,
          diagnostic: (found.doc.status === 'failed' || found.doc.status === 'partial') ?
            found.doc.error?.diagnostic ?? null : null,
          traceId: found.doc.trace_id ?? job.traceId,
          uploadId: found.doc.upload_id ?? job.uploadId,
          }
        })
        return runId ? updated : onlyActiveRuns(updated)
      })
    }, 2000)
    return () => { cancelled = true; controller.abort(); window.clearTimeout(timer) }
  }, [jobs, opening])

  function onTabKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    const nextTab: ResultTab = event.key === 'ArrowRight' ? (tab === 'summary' ? 'pages' : 'summary') :
      (tab === 'pages' ? 'summary' : 'pages')
    setTab(nextTab)
    const buttons = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
    buttons?.[nextTab === 'summary' ? 0 : 1]?.focus()
  }

  // When a selected run finishes, select the next active run (if any).
  useEffect(() => {
    if (runId || restoringUploads) return
    if (jobs.some(job => job.id === selectedId)) return
    setSelectedId(jobs[0]?.id ?? null)
  }, [jobs, selectedId, runId, restoringUploads])
  const selected = jobs.find(job => job.id === selectedId) ?? null
  const selectedStatus = selected?.status
  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    setRunMeta(null); setMetaError(null)
    void catalogueJson(`/api/v1/runs/${selectedId}`, { signal: controller.signal }).then(raw => {
      const parsed = parseRun(raw, selectedId)
      if (!controller.signal.aborted) setRunMeta(parsed)
    }).catch(error => { if (!controller.signal.aborted) setMetaError(error instanceof Error ? error.message : 'Run metadata unavailable.') })
    return () => controller.abort()
  }, [selectedId, selectedStatus, reload])
  const activeCount = jobs.filter(job => isActiveRun(job.status)).length
  const parsedCount = selected?.pages?.length ?? 0
  const currentPage = selected?.pages?.[Math.min(pageIndex, Math.max(0, parsedCount - 1))] ?? null
  const canViewPages = selected?.pages !== null && Boolean(selected?.pages?.length)

  return <main className="container main-content">
      {runId ? <div className="catalogue-heading"><div><a className="catalogue-back" href="/">← Processing History</a><h1>Processing run</h1></div><button className="secondary-button" type="button" disabled={opening || !!retrying} onClick={()=>setReload(n=>n+1)}>Reload result</button></div> :
      <section className="hero" aria-labelledby="hero-heading">
        <div className="eyebrow"><span className="eyebrow-line"/> DOCUMENT INTELLIGENCE PIPELINE</div>
        <h1 id="hero-heading">From PDF to <span className="gradient-text">understanding.</span></h1>
        <p>Extract text, preserve structure, and get a clear AI summary. Your documents are processed asynchronously and ready when you are.</p>
        <div className="hero-tags" aria-label="Project capabilities">
          <span><Icon name="bolt" size={14}/> Async processing</span>
          <span><Icon name="layers" size={14}/> Content-aware cache</span>
          <span><Icon name="check" size={14}/> Worker failover</span>
        </div>
      </section>}

      <div className={runId ? "workspace-grid run-only" : "workspace-grid"}>
        {!runId && <section className="panel upload-panel" aria-labelledby="upload-heading">
          <div className="panel-heading">
            <span className="panel-icon"><Icon name="upload" size={20}/></span>
            <div><div className="panel-overline">01 / INPUT</div><h2 id="upload-heading">New processing job</h2></div>
          </div>
          <form onSubmit={upload}>
            <FileDropzone files={files} disabled={uploading} onFiles={selectFiles}/>
            <div className="form-heading"><span>PROCESSING METHOD</span><span className="form-hint">Select one</span></div>
            <div className="parser-grid" role="radiogroup" aria-label="Processing method">
              {PARSERS.map(item => <label key={item.id} className={`parser-option ${parser === item.id ? 'selected' : ''}`}>
                <input type="radio" name="parser" value={item.id} checked={parser === item.id}
                  onChange={() => setParser(item.id)} disabled={uploading || item.disabled === true}/>
                <span className="parser-icon"><Icon name={item.id === 'pypdf' ? 'file' : item.id === 'gemini' ? 'spark' : 'scan'} size={20}/></span>
                <span className="parser-option-copy"><strong>{item.name}</strong><small>{item.subtitle}</small></span>
                <span className="parser-tag">{item.tag}</span>
                <span className="parser-description">{item.description}</span>
              </label>)}
            </div>
            {uploadError && <div role="alert" className="notice error"><Icon name="warning" size={19}/><span>{uploadError}</span></div>}
            <button className="primary-button" type="submit" disabled={uploading || !files.length || Boolean(validateFiles(files))}>
              {uploading ? 'Submitting documents…' : (files.length === 1 && files[0].name.toLowerCase().endsWith('.zip')) ? 'Process ZIP archive' : `Process ${files.length ? (files.length === 1 ? 'document' : `${files.length} documents`) : 'documents'}`}
              {!uploading && <Icon name="arrow" size={18}/>} {uploading && <span className="button-spinner" aria-hidden="true"/>}
            </button>
            <p className="form-footnote">Summaries use the configured DeepSeek model via OpenRouter. AI calls may incur charges.</p>
          </form>
        </section>}

        <section className="panel results-panel" aria-labelledby="jobs-heading">
          <div className="panel-heading results-heading">
            <span className="panel-icon results-icon"><Icon name="folder" size={20}/></span>
            <div><div className="panel-overline">02 / OUTPUT</div><h2 id="jobs-heading">{runId ? "Archived result" : "Currently processing"}</h2></div>
            <span className="count-chip">{jobs.length} {jobs.length === 1 ? 'FILE' : 'FILES'}</span>
          </div>
          {jobs.length > 0 && <div className="stats-strip" aria-label="Document statistics">
            <div><strong>{activeCount}</strong><span>In progress</span></div>
            <div><strong>{jobs.filter(job => job.status === 'queued').length}</strong><span>Queued</span></div>
            <div><strong>{jobs.filter(job => job.status === 'processing').length}</strong><span>Extracting</span></div>
            <div><strong>{jobs.filter(job => job.status === 'summarizing').length}</strong><span>Summarizing</span></div>
          </div>}
          {pollError && <div className="notice warning" role="status"><Icon name="warning" size={17}/><span>{pollError}</span></div>}
          {opening || (!runId && restoringUploads) ? <div className="results-empty" role="status">{runId ? 'Loading archived result…' : 'Checking active uploads…'}</div> : jobs.length === 0 ? <div className="results-empty">
            <span className="empty-illustration"><Icon name="layers" size={38}/></span>
            <h3>{runId ? 'Run could not be opened' : 'No active processing jobs'}</h3>
            <p>{runId ? 'Reload this result or return to Processing History.' : 'Only queued, processing, and summarizing PDFs appear here. Completed, partial, and failed runs are available in Processing History.'}</p>
            <a href="/" className="secondary-button">Processing History</a>
          </div> : <>
            {!runId && <div className="job-list" aria-label="Currently processing documents">
              {jobs.map(job => <button key={job.id} type="button"
                className={`job-row ${selectedId === job.id ? 'active' : ''}`}
                aria-pressed={selectedId === job.id}
                onClick={() => { setSelectedId(job.id); setTab('summary'); setPageIndex(0) }}>
                <span className="job-file-icon"><Icon name="file" size={19}/></span>
                <span className="job-info"><strong title={job.filename}>{job.filename}</strong>
                  <small>{PARSERS.find(item => item.id === job.parser)?.name} {job.pageCount ? `· ${job.pageCount} pages` : job.pageProgress ? `· Page ${job.pageProgress.page} of ${job.pageProgress.pages_total}` : ''}</small></span>
                <span className="job-trailing"><StatusPill status={job.status}/>
                  {job.cache === 'hit' && <span className="cache-label">↗ CACHE HIT</span>}
                  {job.cache === 'shared' && <span className="cache-label">↗ SHARED JOB</span>}
                </span>
                <Icon name="chevron" size={16}/>
              </button>)}
            </div>}
            {selected && <section className="document-detail" aria-labelledby="detail-heading">
              <div className="detail-header">
                <div className="detail-heading-wrap">
                  <div className="panel-overline">SELECTED RUN</div>
                  <h3 id="detail-heading" title={selected.filename}>{selected.filename}</h3>
                  <div className="detail-meta"><span>{PARSERS.find(item => item.id === selected.parser)?.name}</span>
                    {(selected.pageCount || selected.pageProgress) && <span>{selected.pageCount ?? selected.pageProgress?.pages_total} pages</span>}
                    {selected.cache === 'hit' && <span className="detail-cache">Cache hit</span>}
                    {selected.cache === 'shared' && <span className="detail-cache">Shared processing</span>}
                  </div>
                </div>
                <StatusPill status={selected.status}/>
              </div>
              {!runId && <div className="catalogue-actions run-link"><a className="secondary-button" href={`/runs/${selected.id}`}>Open permanent run</a><a className="secondary-button" href="/">All processing requests</a></div>}
              {runMeta?.run_id === selected.id && <RunMetadata run={runMeta}/>}
              {metaError && <p className="notice warning" role="status">Additional metadata unavailable: {metaError}</p>}
              <TraceDetails documentId={selected.id} traceId={selected.traceId} uploadId={selected.uploadId}
                grafanaUrl={import.meta.env.VITE_GRAFANA_URL} datasourceUid={import.meta.env.VITE_GRAFANA_TEMPO_UID || 'tempo'}/>
              <div className="pipeline" aria-label="Processing stages">
                {processingSteps(selected.status).map((step, i) => <div className={`pipeline-step ${step.state}`} key={step.label}>
                  <span className="step-marker">{step.state === 'done' ? <Icon name="check" size={15}/> : step.state === 'error' ? '!' : i + 1}</span>
                  <span>{step.label}</span>
                </div>)}
              </div>
              {selected.status === 'failed' && <div className="notice error" role="alert"><Icon name="warning" size={19}/><span>{selected.error ?? 'Document processing failed.'}</span></div>}
              {selected.status === 'partial' && <div className="notice partial-notice" role="status">
                <Icon name="warning" size={20}/><div className="notice-content"><strong>Extraction complete; summary unavailable</strong>
                  <p>{selected.error ?? 'The summary request failed. Your extracted pages are safe.'}</p>
                  <button type="button" className="retry-button" disabled={retrying === selected.id}
                    onClick={() => void retrySummary(selected.id)}><Icon name="repeat" size={15}/>{retrying === selected.id ? 'Retrying…' : 'Retry summary only'}</button>
                </div>
              </div>}
              {selected.diagnostic && (selected.status === 'failed' || selected.status === 'partial') &&
                <details className="diagnostics-detail">
                  <summary>Technical details</summary>
                  <dl className="diagnostics-grid">{diagnosticEntries(selected.diagnostic).map(([label, value]) =>
                    <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
                  )}</dl>
                </details>}
              {['queued', 'processing', 'summarizing'].includes(selected.status) && <div className="processing-note" role="status">
                <span className="activity-pulse"/><span>{selected.status === 'queued' ? 'Waiting for an available worker…' : selected.status === 'processing' ? (selected.pageProgress
                  ? `Extracting page ${selected.pageProgress.page} of ${selected.pageProgress.pages_total}${selected.pageProgress.attempt > 1 ? ` (retry ${selected.pageProgress.attempt}/${selected.pageProgress.max_attempts})` : ''}…`
                  : 'Extracting document pages…') : 'Generating the document summary…'}</span>
              </div>}
              {(selected.status !== 'failed') && <>
                <div className="detail-tabs" role="tablist" aria-label="Document results">
                  <button type="button" role="tab" aria-selected={tab === 'summary'} tabIndex={tab === 'summary' ? 0 : -1}
                    aria-controls="document-tab-panel" onKeyDown={onTabKeyDown} className={tab === 'summary' ? 'selected' : ''}
                    onClick={() => setTab('summary')}>AI Summary</button>
                  <button type="button" role="tab" aria-selected={tab === 'pages'} tabIndex={tab === 'pages' ? 0 : -1}
                    aria-controls="document-tab-panel" onKeyDown={onTabKeyDown} className={tab === 'pages' ? 'selected' : ''}
                    onClick={() => setTab('pages')}>Extracted pages{selected.pageCount ? ` (${selected.pageCount})` : ''}</button>
                </div>
                <div className="preview-wrap" role="tabpanel" id="document-tab-panel">
                  {tab === 'summary' ? selected.status === 'completed' && selected.summary ? <>
                    <div className="preview-top"><span><Icon name="spark" size={16}/> SUMMARY</span>
                      <a className="secondary-button" href={`/api/v1/runs/${selected.id}/summary`}>
                        <Icon name="download" size={15}/> Download .md</a>
                    </div>
                    <div className="preview-body"><Markdown text={selected.summary}/></div>
                  </> : <div className="preview-placeholder"><Icon name="spark" size={23}/><strong>{selected.status === 'partial' ? 'Summary could not be generated' : 'Summary pending'}</strong>
                    <p>{selected.status === 'partial' ? 'Your extracted pages are available in the next tab.' : 'The summary will appear here when processing finishes.'}</p></div>
                  : canViewPages && currentPage ? <>
                    <div className="preview-top"><span><Icon name="file" size={16}/> EXTRACTED MARKDOWN</span>
                      <a className="secondary-button" href={`/api/v1/runs/${selected.id}/markdown`}>
                        <Icon name="download" size={15}/> Download .md</a>
                      {selected.parser === 'vision_figures' && <a className="secondary-button" href={`/api/v1/runs/${selected.id}/bundle.zip`}>
                        <Icon name="download" size={15}/> Markdown + pictures .zip</a>}
                    </div>
                    <div className="page-navigation">
                      <label htmlFor="page-select">PAGE</label>
                      <select id="page-select" value={Math.min(pageIndex, parsedCount - 1)} onChange={event => setPageIndex(Number(event.target.value))}>
                        {selected.pages!.map((page, i) => <option key={page.page} value={i}>Page {page.page} of {parsedCount}</option>)}
                      </select>
                      <span className="page-nav-spacer"/>
                      <button type="button" className="page-nav-btn" disabled={pageIndex <= 0} aria-label="Previous page" onClick={() => setPageIndex(Math.max(0, pageIndex - 1))}>‹</button>
                      <button type="button" className="page-nav-btn" disabled={pageIndex >= parsedCount - 1} aria-label="Next page" onClick={() => setPageIndex(Math.min(parsedCount - 1, pageIndex + 1))}>›</button>
                    </div>
                    <div className="preview-body">{currentPage.markdown.trim() ? <Markdown text={currentPage.markdown} imageRunId={selected.parser === 'vision_figures' ? selected.id : undefined}/> : <p className="blank-page">This page is blank.</p>}</div>
                  </> : <div className="preview-placeholder"><Icon name="file" size={23}/><strong>Pages not available yet</strong><p>Pages will appear as soon as extraction is saved.</p></div>}
                </div>
              </>}
            </section>}
          </>}
        </section>
      </div>
      <div className="architecture-note"><Icon name="layers" size={17}/><span>Powered by <strong>Redis Streams</strong> · <strong>2 Python workers</strong> · <strong>RustFS S3</strong> · <strong>OpenRouter</strong></span></div>
    </main>
}


export default function App() {
  const [path,setPath]=useState(()=>window.location.pathname)
  useEffect(()=>{
    const onPopState=()=>setPath(window.location.pathname)
    window.addEventListener('popstate',onPopState)
    return ()=>window.removeEventListener('popstate',onPopState)
  },[])
  function navigate(next:string) { if(next!==path){window.history.pushState(null,'',next);setPath(next);window.scrollTo(0,0)} }
  const route=routeFor(path)
  const nav=[{href:'/',label:'History',active:route.view==='history'||route.view==='run'}, {href:'/upload',label:'Upload',active:route.view==='upload'}, {href:'/library',label:'Library',active:route.view==='library'||route.view==='document'}, {href:'/search',label:'Search',active:route.view==='search'}, {href:'/system',label:'System Monitor',active:route.view==='system'}]
  return <div className="app-shell">
    <div className="background-glow" aria-hidden="true" />
    <header className="topbar container">
      <div className="brand">
        <span className="brand-mark"><Icon name="layers" size={23}/></span>
        <div className="brand-name">PDF<span>Processor</span><span className="version-mark">v{appVersion}</span></div>
      </div>
      <nav className="app-nav" aria-label="Main navigation">
        {nav.map(item => <a key={item.href} href={item.href} className={item.active ? 'active' : ''} aria-current={item.active ? 'page' : undefined}
          onClick={event => { if (event.button===0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {event.preventDefault(); navigate(item.href)} }}>{item.label}</a>)}
      </nav>
    </header>
    {route.view === 'search' ? <Search/> : route.view === 'system' ? <SystemMonitor/> : route.view === 'history' ? <History/> : route.view === 'library' ? <Library/> : route.view === 'document' && 'id' in route ? <LibraryDetail key={path} id={route.id}/> : route.view === 'run' && 'id' in route ? <Workspace key={path} runId={route.id}/> : route.view === 'upload' ? null : <main className="container catalogue-main"><h1>Page not found</h1><a href="/">Return to Processing History</a></main>}
    {/* Keep the Upload component alive while visiting History/Library/Search/System so its
        selected files, in-flight POST and polling survive client-side navigation. */}
    <div hidden={route.view !== 'upload'}><Workspace/></div>
    <footer className="footer container"><span>PDF Processor · Local developer workspace</span><span>Archived results survive refresh and service restarts. New development installations reset data.</span></footer>
  </div>
}
