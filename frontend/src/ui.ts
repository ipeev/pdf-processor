import type { Page, Parser, Status } from './contracts'

export const PARSERS: { id: Parser; name: string; subtitle: string; description: string; tag: string; disabled?: boolean }[] = [
  { id: 'pypdf', name: 'PyPDF', subtitle: 'Native text', description: 'Fast local extraction for searchable PDFs.', tag: 'FASTEST' },
  { id: 'gemini', name: 'AI PDF', subtitle: 'Configurable model', description: 'Native PDF extraction into structured Markdown; model configurable.', tag: 'STRUCTURED' },
  { id: 'gpt5_mini', name: 'Image OCR', subtitle: 'Configurable model', description: 'Visually extract text from scanned documents; model configurable.', tag: 'VISION' },
  { id: 'vision_figures', name: 'AI OCR + Pictures', subtitle: 'Full-page vision extraction', description: 'Combined vision transcription and original figure crops, with downloadable images.', tag: 'NEW' },
]

export const STATUS_LABELS: Record<Status, string> = {
  queued: 'Queued', processing: 'Extracting', summarizing: 'Summarizing',
  completed: 'Completed', partial: 'Partial result', failed: 'Failed',
}

export type StepState = 'done' | 'current' | 'waiting' | 'error'
export type ProcessingStep = { label: string; state: StepState }

/** Stage indicators are NOT a fabricated percentage completion estimate. */
export function processingSteps(status: Status): ProcessingStep[] {
  const states: Record<Status, StepState[]> = {
    queued: ['done', 'waiting', 'waiting'],
    processing: ['done', 'current', 'waiting'],
    summarizing: ['done', 'done', 'current'],
    completed: ['done', 'done', 'done'],
    partial: ['done', 'done', 'error'],
    failed: ['done', 'error', 'waiting'],
  }
  return ['Accepted', 'Extract', 'Summarize'].map((label, i) => ({ label, state: states[status][i] }))
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KiB` : `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

/** Filename is only for a local download, never used as a server path. */
export function markdownFilename(filename: string, suffix: 'summary' | 'pages'): string {
  const base = filename.replace(/\.pdf$/i, '').replace(/[^a-zA-Z0-9._ -]/g, '_')
    .replace(/^[._ -]+/, '').trim().slice(0, 80) || 'document'
  return `${base}-${suffix}.md`
}

export function pagesAsMarkdown(pages: Page[]): string {
  return pages.map(page => `# Page ${page.page}\n\n${page.markdown.trim() || '_(Blank page)_'}`).join('\n\n---\n\n') + '\n'
}

/** Reject navigation to script/data/file URLs found inside uploaded documents. */
export function safeMarkdownUrl(url: string, origin: string): string {
  try {
    const resolved = new URL(url, origin)
    return ['https:', 'http:', 'mailto:'].includes(resolved.protocol) ? url : ''
  } catch {
    return ''
  }
}
