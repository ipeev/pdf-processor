import type { Status } from './contracts.ts'
import { isRunId } from './catalogue.ts'

// Only pending work belongs on Upload. Every terminal outcome belongs in History.
export const ACTIVE_RUN_STATUSES: readonly Status[] = ['queued', 'processing', 'summarizing']
export const isActiveRun = (status: Status): boolean => ACTIVE_RUN_STATUSES.includes(status)
export const onlyActiveRuns = <T extends { status: Status }>(runs: readonly T[]): T[] => runs.filter(run => isActiveRun(run.status))

const KEY = 'pdf-processor.active-upload-run-ids.v1'
const MAX_TRACKED = 200

// Store IDs only; filenames, results and document contents remain on the server.
// sessionStorage is optional (e.g., disabled by a browser's privacy settings).
export function readActiveUploadIds(storage: Pick<Storage, 'getItem'>): string[] {
  try {
    const value: unknown = JSON.parse(storage.getItem(KEY) ?? '[]')
    if (!Array.isArray(value)) return []
    return [...new Set(value.filter(isRunId))].slice(0, MAX_TRACKED)
  } catch { return [] }
}
export function writeActiveUploadIds(storage: Pick<Storage, 'setItem'>, ids: readonly string[]): void {
  try { storage.setItem(KEY, JSON.stringify([...new Set(ids.filter(isRunId))].slice(0, MAX_TRACKED))) }
  catch { /* Browsers may block sessionStorage; in-page tracking still works. */ }
}
