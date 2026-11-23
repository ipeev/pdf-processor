/** Defensive parsing of the local read-only monitoring endpoint. */
export type ServiceStatus = 'online' | 'unavailable'
export type WorkerStage = 'idle' | 'extracting' | 'summarizing'
export type WorkerTelemetry = { id: string; state: WorkerStage; parser: 'pypdf' | 'gemini' | 'gpt5_mini' | 'vision_figures' | null; container: string | null }
export type DockerPoint = { timestamp: string; cpuPercent: number | null; memoryUsedBytes: number | null }
export type DockerContainer = {
  service: 'api' | 'frontend' | 'redis' | 'rustfs' | 'worker' | 'opensearch'; replica: number; id: string
  state: string; health: string; memoryUsedBytes: number | null; memoryLimitBytes: number | null
  cpuPercent: number | null; cpuQuota: number | null; cpusetCpus: number | null
  processes: number | null; threads: number | null; history: DockerPoint[]
}
const archiveStates = ['queued', 'processing', 'summarizing', 'completed', 'partial', 'failed'] as const
const archiveCache = ['miss', 'hit', 'shared'] as const
export type ArchiveView = {
  status: ServiceStatus; checkedAt: string | null; truncated: boolean
  objects: number | null; bytes: number | null; originals: number | null; originalBytes: number | null
  artifactBytes: number | null; metadataBytes: number | null; documents: number | null; runs: number | null
  indexedDocuments: number | null; indexedRuns: number | null; duplicateSubmissions: number | null; reusedOriginalRuns: number | null
  runStatus: Record<typeof archiveStates[number], number | null>; runCache: Record<typeof archiveCache[number], number | null>
  consistency: 'unknown' | 'attention' | 'counts_match' | 'count_mismatch'
  deletionPending: boolean | null; reconciliationNeeded: boolean | null; restorePending: boolean | null
  recovery: { state: 'unknown' | 'completed' | 'deferred'; checkedAt: string | null; recovered: number | null }
}
export type SystemSnapshot = {
  archive: ArchiveView
  docker: { status: ServiceStatus; collectedAt: string | null; containers: DockerContainer[] }

  generatedAt: string
  api: { status: 'online' }
  redis: {
    status: ServiceStatus
    memoryBytes: number | null
    memoryLimitBytes: number | null
    keyCount: number | null
    queue: { waiting: number | null; pending: number | null }
    workers: WorkerTelemetry[]
    cache: { hits: number | null; misses: number | null; shared: number | null }
  }
  rustfs: { status: ServiceStatus; objects: number | null; bytes: number | null; truncated: boolean }
  opensearch: { status: ServiceStatus; clusterHealth: 'green' | 'yellow' | 'red' | null; heapUsedBytes: number | null; heapMaxBytes: number | null; indexStoreBytes: number | null; nodeCount: number | null }
  openrouter: {
    status: 'available' | 'not_monitored' | 'not_configured' | 'unavailable' | 'auth_error' | 'rate_limited'
    checkedAt: string | null
    usageUsd: number | null
    usageDailyUsd: number | null
    usageWeeklyUsd: number | null
    usageMonthlyUsd: number | null
    keyLimitUsd: number | null
    keyLimitRemainingUsd: number | null
    keyLimitReset: 'daily' | 'weekly' | 'monthly' | null
    budgetLimitUsd: number | null
    budgetRemainingUsd: number | null
    budgetScope: 'key' | 'member' | 'workspace' | 'organization' | null
    budgetResetsAt: string | null
    expiresAt: string | null
    privacyPolicy: 'not_verifiable_by_key_api'
  }
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('Invalid monitoring response.')
  return value as Record<string, unknown>
}
function status(value: unknown): ServiceStatus {
  if (value === 'online' || value === 'unavailable') return value
  throw new Error('Invalid monitoring service status.')
}
function count(value: unknown): number | null {
  if (value === null) return null
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) throw new Error('Invalid monitoring count.')
  return value
}
function moneyOrNull(value: unknown): number | null {
  if (value == null) return null
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error('Invalid key usage amount.')
  return value
}
function optionalDate(value: unknown): string | null {
  if (value == null) return null
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) throw new Error('Invalid timestamp.')
  return value
}
function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T | null {
  if (value == null) return null
  if (typeof value === 'string' && allowed.includes(value as T)) return value as T
  throw new Error('Invalid key telemetry field.')
}
export function usd(value: number | null): string {
  return value == null ? '—' : `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`
}
function cpuOrNull(value: unknown): number | null {
  if (value === null) return null
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 64000) throw new Error('Invalid Docker CPU metric.')
  return value
}
function dockerData(raw: unknown): SystemSnapshot['docker'] {
  if (raw === undefined) return { status: 'unavailable', collectedAt: null, containers: [] }
  const section = record(raw)
  if (!Array.isArray(section.containers) || section.containers.length > 16) throw new Error('Invalid Docker telemetry.')
  const containers: DockerContainer[] = section.containers.map(entry => {
    const item = record(entry)
    if (!['api', 'frontend', 'redis', 'rustfs', 'worker', 'opensearch'].includes(String(item.service)) ||
        typeof item.id !== 'string' || !/^[0-9a-f]{12}$/.test(item.id) ||
        typeof item.state !== 'string' || !['running', 'exited', 'created', 'paused', 'restarting', 'dead', 'unknown'].includes(item.state) ||
        typeof item.health !== 'string' || !['healthy', 'unhealthy', 'starting', 'none', 'unknown'].includes(item.health) ||
        !Array.isArray(item.history) || item.history.length > 65) throw new Error('Invalid Docker container.')
    const replica = count(item.replica)
    if (replica == null || replica < 1 || replica > 32 || (item.service !== 'worker' && replica !== 1)) throw new Error('Invalid container replica.')
    return {
      service: item.service as DockerContainer['service'], replica, id: item.id,
      state: item.state, health: item.health, memoryUsedBytes: count(item.memory_used_bytes),
      memoryLimitBytes: count(item.memory_limit_bytes), cpuPercent: cpuOrNull(item.cpu_percent),
      cpuQuota: cpuOrNull(item.cpu_quota), cpusetCpus: count(item.cpuset_cpus),
      processes: count(item.processes), threads: count(item.threads),
      history: item.history.map((point: unknown) => {
        const observation = record(point)
        return { timestamp: optionalDate(observation.timestamp) ?? (() => { throw new Error('Invalid CPU sample time.') })(),
          cpuPercent: cpuOrNull(observation.cpu_percent), memoryUsedBytes: count(observation.memory_used_bytes) }
      }),
    }
  })
  return { status: status(section.status), collectedAt: optionalDate(section.collected_at), containers }
}
function stage(value: unknown): WorkerStage {
  if (value === 'idle' || value === 'extracting' || value === 'summarizing') return value
  throw new Error('Invalid worker stage.')
}
function parser(value: unknown): WorkerTelemetry['parser'] {
  if (value === null || value === 'pypdf' || value === 'gemini' || value === 'gpt5_mini' || value === 'vision_figures') return value
  throw new Error('Invalid worker parser.')
}
function archiveData(value: unknown): ArchiveView {
  const data = value === undefined ? {} : record(value)
  const recovery = data.recovery === undefined ? {} : record(data.recovery)
  const states = data.run_status === undefined ? {} : record(data.run_status)
  const cache = data.run_cache === undefined ? {} : record(data.run_cache)
  function flag(value: unknown): boolean | null {
    if (value == null) return null
    if (typeof value !== 'boolean') throw new Error('Invalid archive flag.')
    return value
  }
  return { status: status(data.status ?? 'unavailable'), checkedAt: optionalDate(data.checked_at), truncated: flag(data.truncated) ?? false,
    objects: count(data.objects ?? null), bytes: count(data.bytes ?? null), originals: count(data.originals ?? null),
    originalBytes: count(data.original_bytes ?? null), artifactBytes: count(data.artifact_bytes ?? null), metadataBytes: count(data.metadata_bytes ?? null),
    documents: count(data.documents ?? null), runs: count(data.runs ?? null), indexedDocuments: count(data.indexed_documents ?? null),
    indexedRuns: count(data.indexed_runs ?? null), duplicateSubmissions: count(data.duplicate_submissions ?? null), reusedOriginalRuns: count(data.reused_original_runs ?? null),
    runStatus: Object.fromEntries(archiveStates.map(k => [k, count(states[k] ?? null)])) as ArchiveView['runStatus'],
    runCache: Object.fromEntries(archiveCache.map(k => [k, count(cache[k] ?? null)])) as ArchiveView['runCache'],
    consistency: oneOf(data.consistency, ['unknown', 'attention', 'counts_match', 'count_mismatch'] as const) ?? 'unknown',
    deletionPending: flag(data.deletion_pending), reconciliationNeeded: flag(data.reconciliation_needed), restorePending: flag(data.restore_pending),
    recovery: { state: oneOf(recovery.state, ['unknown', 'completed', 'deferred'] as const) ?? 'unknown', checkedAt: optionalDate(recovery.checked_at), recovered: count(recovery.recovered ?? null) },
  }
}
export function parseSystemSnapshot(input: unknown): SystemSnapshot {
  const root = record(input)
  const api = record(root.api)
  const redis = record(root.redis)
  const queue = record(redis.queue)
  const cache = record(redis.cache)
  const rustfs = record(root.rustfs)
  const openrouter = record(root.openrouter)
  const search = root.opensearch === undefined ? { status: 'unavailable', cluster_health: null, heap_used_bytes: null, heap_max_bytes: null, index_store_bytes: null, node_count: null } : record(root.opensearch)
  const workers = redis.workers
  if (api.status !== 'online' || !['available', 'not_monitored', 'not_configured', 'unavailable', 'auth_error', 'rate_limited'].includes(String(openrouter.status)) ||
      typeof root.generated_at !== 'string' || !Number.isFinite(Date.parse(root.generated_at)) ||
      !Array.isArray(workers) || workers.length > 128 || typeof rustfs.truncated !== 'boolean') {
    throw new Error('Invalid monitoring response.')
  }
  return {
    generatedAt: root.generated_at,
    archive: archiveData(root.archive),
    docker: dockerData(root.docker),
    api: { status: 'online' },
    redis: {
      status: status(redis.status), memoryBytes: count(redis.memory_bytes),
      memoryLimitBytes: count(redis.memory_limit_bytes), keyCount: count(redis.key_count),
      queue: { waiting: count(queue.waiting), pending: count(queue.pending) },
      workers: workers.map((value: unknown) => {
        const worker = record(value)
        if (typeof worker.id !== 'string' || worker.id.length > 16 || !/^[\w-]+$/.test(worker.id)) throw new Error('Invalid worker identity.')
        if (worker.container != null && (typeof worker.container !== 'string' || !/^[0-9a-f]{8}$/.test(worker.container))) throw new Error('Invalid worker container ID.')
        return { id: worker.id, state: stage(worker.state), parser: parser(worker.parser), container: worker.container ?? null }
      }),
      cache: { hits: count(cache.hits), misses: count(cache.misses), shared: count(cache.shared) },
    },
    rustfs: { status: status(rustfs.status), objects: count(rustfs.objects), bytes: count(rustfs.bytes), truncated: rustfs.truncated },
    opensearch: { status: status(search.status), clusterHealth: oneOf(search.cluster_health, ['green', 'yellow', 'red'] as const),
      heapUsedBytes: count(search.heap_used_bytes), heapMaxBytes: count(search.heap_max_bytes), indexStoreBytes: count(search.index_store_bytes), nodeCount: count(search.node_count) },
    openrouter: {
      status: openrouter.status as SystemSnapshot['openrouter']['status'],
      checkedAt: optionalDate(openrouter.checked_at),
      usageUsd: moneyOrNull(openrouter.usage_usd),
      usageDailyUsd: moneyOrNull(openrouter.usage_daily_usd),
      usageWeeklyUsd: moneyOrNull(openrouter.usage_weekly_usd),
      usageMonthlyUsd: moneyOrNull(openrouter.usage_monthly_usd),
      keyLimitUsd: moneyOrNull(openrouter.key_limit_usd),
      keyLimitRemainingUsd: moneyOrNull(openrouter.key_limit_remaining_usd),
      keyLimitReset: oneOf(openrouter.key_limit_reset, ['daily', 'weekly', 'monthly'] as const),
      budgetLimitUsd: moneyOrNull(openrouter.budget_limit_usd),
      budgetRemainingUsd: moneyOrNull(openrouter.budget_remaining_usd),
      budgetScope: oneOf(openrouter.budget_scope, ['key', 'member', 'workspace', 'organization'] as const),
      budgetResetsAt: optionalDate(openrouter.budget_resets_at),
      expiresAt: optionalDate(openrouter.expires_at),
      privacyPolicy: openrouter.privacy_policy === undefined || openrouter.privacy_policy === 'not_verifiable_by_key_api'
        ? 'not_verifiable_by_key_api' : (() => { throw new Error('Invalid privacy policy metadata.') })(),
    },
  }
}
export function compactBytes(value: number | null): string {
  if (value === null) return '—'
  if (value < 1024) return `${value} B`
  const unit = value < 1024 ** 2 ? 'KiB' : value < 1024 ** 3 ? 'MiB' : 'GiB'
  const divisor = unit === 'KiB' ? 1024 : unit === 'MiB' ? 1024 ** 2 : 1024 ** 3
  return `${(value / divisor).toFixed(value >= divisor * 10 ? 0 : 1)} ${unit}`
}
export function countLabel(value: number | null): string { return value === null ? '—' : value.toLocaleString('en-US') }
export function stageLabel(value: WorkerStage): string {
  return value === 'idle' ? 'Idle' : value === 'extracting' ? 'Extracting PDF' : 'Summarizing'
}
export function modelLabel(value: WorkerTelemetry['parser']): string {
  return value === 'vision_figures' ? 'AI OCR + Pictures' : value === 'gpt5_mini' ? 'Image OCR' : value === 'gemini' ? 'AI PDF' : value === 'pypdf' ? 'PyPDF' : 'Awaiting a job'
}
