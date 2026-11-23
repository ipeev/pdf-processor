import { useEffect, useState } from 'react'
import { copyIdentifier, grafanaTraceUrl } from './traceLinks'
import { validTraceId } from './contracts'

export default function TraceDetails({ documentId, traceId, uploadId, grafanaUrl, datasourceUid = 'tempo' }: {
  documentId: string
  traceId: string | null
  uploadId: string | null
  grafanaUrl?: string
  datasourceUid?: string
}) {
  const [feedback, setFeedback] = useState('')
  useEffect(() => setFeedback(''), [documentId, traceId])
  const safeTrace = validTraceId(traceId)
  const traceUrl = grafanaTraceUrl(grafanaUrl, safeTrace, datasourceUid)
  async function copy(value: string, label: string) {
    const succeeded = await copyIdentifier(value)
    setFeedback(succeeded ? `${label} copied.` : `Couldn't copy ${label.toLowerCase()}; select the ID manually.`)
  }

  return <section className="trace-details" aria-label="Processing identifiers">
    <div className="trace-details-header"><strong>Processing trace</strong><span>Logs and performance diagnostics</span></div>
    <div className="trace-fields">
      <div className="trace-field">
        <span className="trace-label">Trace ID</span>
        {safeTrace ? <>
          <code className="trace-value" title={safeTrace}>{safeTrace}</code>
          <div className="trace-actions">
            <button type="button" onClick={() => void copy(safeTrace, 'Trace ID')}>Copy Trace ID</button>
            {traceUrl ? <a href={traceUrl} target="_blank" rel="noopener noreferrer">View in Grafana ↗</a> :
              <span className="trace-hint">Grafana → Explore → Tempo → Trace ID</span>}
          </div>
        </> : <span className="trace-missing">Trace unavailable</span>}
      </div>
      <div className="trace-field">
        <span className="trace-label">Run ID (legacy Document ID)</span>
        <code className="trace-value" title={documentId}>{documentId}</code>
        <div className="trace-actions"><button type="button" onClick={() => void copy(documentId, 'Run ID')}>Copy Run ID</button></div>
      </div>
      {uploadId && <div className="trace-field trace-upload">
        <span className="trace-label">Upload ID</span>
        <code className="trace-value">{uploadId}</code>
        <span className="trace-hint">PDFs from the same ZIP upload may share a trace.</span>
      </div>}
    </div>
    {feedback && <p role="status" className="trace-feedback">{feedback}</p>}
    <p className="trace-footnote">A Trace ID is not proof the spans reached Tempo; traces expire after the observability retention period.</p>
  </section>
}
