/** Browser-facing Grafana trace links. Never use internal Docker URLs in the browser. */
import { validTraceId } from './contracts.ts'

const datasourcePattern = /^[A-Za-z0-9_-]{1,100}$/

/** Null means Grafana is unconfigured or the value is unsafe for a hyperlink. */
export function grafanaTraceUrl(base: string | undefined, traceId: string | null, datasourceUid = 'tempo'): string | null {
  const safeTrace = validTraceId(traceId)
  if (!safeTrace || !base?.trim() || !datasourcePattern.test(datasourceUid)) return null
  try {
    const url = new URL(base.trim())
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null
    // Grafana also supports a subpath deployment (e.g. /grafana/).
    url.pathname = `${url.pathname.replace(/\/$/, '')}/explore`
    url.searchParams.set('schemaVersion', '1')
    url.searchParams.set('orgId', '1')
    url.searchParams.set('panes', JSON.stringify({
      trace: {
        datasource: datasourceUid,
        queries: [{ refId: 'A', datasource: { type: 'tempo', uid: datasourceUid }, queryType: 'traceId', query: safeTrace }],
        range: { from: 'now-7d', to: 'now' },
      },
    }))
    return url.toString()
  } catch {
    return null
  }
}

/** Copy IDs in both secure browser contexts and plain-HTTP local deployments. */
export async function copyIdentifier(text: string): Promise<boolean> {
  if (!text) return false
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* Some browsers disallow the Clipboard API outside a secure context. */ }
  try {
    const field = document.createElement('textarea')
    field.value = text
    field.setAttribute('readonly', '')
    field.style.position = 'fixed'
    field.style.left = '-9999px'
    document.body.appendChild(field)
    field.select()
    const ok = document.execCommand('copy')
    field.remove()
    return ok
  } catch {
    return false
  }
}
