import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parseSystemSnapshot, compactBytes, countLabel, stageLabel, modelLabel, usd } from '../src/system.ts'

const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
const monitor = readFileSync(new URL('../src/SystemMonitor.tsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
const good = {
  generated_at: '2026-11-12T13:01:02+00:00', api: { status: 'online' },
  redis: { status: 'online', memory_bytes: 12345, memory_limit_bytes: 268435456,
    key_count: 12, queue: { waiting: 2, pending: 1 },
    workers: [{ id: 'ab1234cd', state: 'summarizing', parser: 'gpt5_mini' }],
    cache: { hits: 3, misses: 5, shared: 2 } },
  rustfs: { status: 'online', objects: 2, bytes: 3000, truncated: false },
  openrouter: { status: 'not_monitored' },
}
test('validates a complete operational snapshot', () => {
  const parsed = parseSystemSnapshot(good)
  assert.equal(parsed.redis.queue.waiting, 2)
  assert.equal(parsed.redis.queue.pending, 1)
  assert.equal(parsed.redis.workers[0].state, 'summarizing')
  assert.equal(parsed.rustfs.objects, 2)
  assert.equal(parsed.openrouter.status, 'not_monitored')
})
test('partial service outages retain unknown metrics rather than inventing zeros', () => {
  const data = parseSystemSnapshot({ ...good,
    redis: { status: 'unavailable', memory_bytes: null, memory_limit_bytes: null, key_count: null,
      queue: { waiting: null, pending: null }, workers: [], cache: { hits: null, misses: null, shared: null } },
    rustfs: { status: 'unavailable', objects: null, bytes: null, truncated: false } })
  assert.equal(data.redis.queue.waiting, null)
  assert.equal(data.redis.keyCount, null)
  assert.equal(data.rustfs.bytes, null)
  assert.equal(countLabel(null), '—')
  assert.equal(compactBytes(null), '—')
})
test('rejects malformed payloads and invalid worker states', () => {
  assert.throws(() => parseSystemSnapshot(null), /Invalid/)
  assert.throws(() => parseSystemSnapshot({ ...good, redis: { ...good.redis, key_count: -1 } }), /Invalid/)
  assert.throws(() => parseSystemSnapshot({ ...good, rustfs: { ...good.rustfs, objects: Infinity } }), /Invalid/)
  assert.throws(() => parseSystemSnapshot({ ...good, redis: { ...good.redis, workers: [{ id:'a', state:'flying', parser: null }] } }), /Invalid/)
  assert.throws(() => parseSystemSnapshot({ ...good, redis: { ...good.redis, workers: [{ id:'../../home', state:'idle', parser: null }] } }), /Invalid/)
})
test('formats worker statuses and capacity data clearly', () => {
  assert.equal(stageLabel('idle'), 'Idle')
  assert.equal(stageLabel('summarizing'), 'Summarizing')
  assert.equal(modelLabel('gpt5_mini'), 'Image OCR')
  assert.equal(compactBytes(2048), '2.0 KiB')
  assert.equal(countLabel(1205), '1,205')
})
test('navigation switches between same-origin Workspace and /system without extra dependencies', () => {
  assert.match(app, /window\.history\.pushState/)
  assert.match(app, /routeFor\(path\)/)
  assert.match(app, /<SystemMonitor\/>/)
  assert.match(app, /aria-label="Main navigation"/)
  assert.match(monitor, /fetch\('\/api\/v1\/system\/status'/)
  assert.match(monitor, /REFRESH_MS = 3000/)
  assert.match(monitor, /controller\.abort\(\)/)
})
test('diagram differentiates live state from schematic topology', () => {
  assert.match(monitor, /arrows are illustrative, not distributed traces/)
  assert.match(monitor, /Inference not probed/)
  assert.match(monitor, /OpenRouterNode/)
  assert.match(monitor, /excludes credentials and document contents/)
  assert.match(css, /\.flow-diagram/)
  assert.match(css, /@media\(max-width:600px\)/)
})


test('validates key-specific OpenRouter usage and effective workspace budget', () => {
  const data = parseSystemSnapshot({ ...good, openrouter: {
    status: 'available', checked_at: '2026-11-13T10:01:00Z',
    usage_usd: 4.15, usage_daily_usd: 0.15, usage_weekly_usd: 0.9,
    usage_monthly_usd: 2.13, key_limit_usd: 10,
    key_limit_remaining_usd: 5.85, key_limit_reset: 'monthly',
    budget_limit_usd: 50, budget_remaining_usd: 30, budget_scope: 'workspace',
    budget_resets_at: null, expires_at: null, privacy_policy: 'not_verifiable_by_key_api',
  } })
  assert.equal(data.openrouter.status, 'available')
  assert.equal(data.openrouter.usageMonthlyUsd, 2.13)
  assert.equal(data.openrouter.budgetScope, 'workspace')
  assert.equal(data.openrouter.privacyPolicy, 'not_verifiable_by_key_api')
  assert.equal(usd(2.13), '$2.13')
  assert.match(monitor, /OpenRouter key usage/)
  assert.doesNotMatch(monitor, /06 \/ PROVIDER BILLING/)
  assert.match(monitor, /not verified by the key endpoint|cannot be verified by this API/)
})

test('unknown key policy and malformed usage are rejected', () => {
  assert.throws(() => parseSystemSnapshot({ ...good, openrouter: { status: 'available', usage_daily_usd: NaN } }), /Invalid/)
  assert.throws(() => parseSystemSnapshot({ ...good, openrouter: { status: 'available', privacy_policy: 'verified_zdr' } }), /Invalid/)
  assert.equal(parseSystemSnapshot({ ...good, openrouter: { status: 'auth_error' } }).openrouter.status, 'auth_error')
})

test('parses bounded Docker metrics and treats absent collector as unavailable', () => {
  const old = parseSystemSnapshot(good)
  assert.equal(old.docker.status, 'unavailable')
  assert.equal(old.docker.containers.length, 0)
  const updated = parseSystemSnapshot({ ...good, docker: { status: 'online', collected_at: '2026-11-13T11:10:00Z', containers: [{
    service: 'worker', replica: 1, id: '123456789abc', state: 'running', health: 'none',
    memory_used_bytes: 1024 * 1024, memory_limit_bytes: null, cpu_percent: 172.4,
    cpu_quota: null, cpuset_cpus: 4, processes: 1, threads: 26,
    history: [{ timestamp: '2026-11-13T11:09:55Z', cpu_percent: 172.4, memory_used_bytes: 1024 * 1024 }],
  }] } })
  assert.equal(updated.docker.containers[0].cpuPercent, 172.4)
  assert.equal(updated.docker.containers[0].threads, 26)
  assert.equal(updated.docker.containers[0].memoryLimitBytes, null)
  assert.equal(updated.docker.containers[0].history.length, 1)
  assert.match(monitor, /Docker resource telemetry/)
  assert.match(monitor, /CpuHistory/)
})
test('rejects invalid Docker measurements rather than rendering fabricated telemetry', () => {
  const data = { ...good, docker: { status: 'online', collected_at: null, containers: [{
    service: 'worker', replica: 1, id: '123456789abc', state: 'running', health: 'none',
    memory_used_bytes: 10, memory_limit_bytes: null, cpu_percent: -1, cpu_quota: null,
    cpuset_cpus: null, processes: 1, threads: 20, history: [],
  }] } }
  assert.throws(() => parseSystemSnapshot(data), /Invalid/)
  assert.throws(() => parseSystemSnapshot({ ...good, docker: { status: 'online', collected_at: null, containers: new Array(17).fill({}) } }), /Invalid/)
})

test('OpenSearch metrics and unavailable fallback are parsed without changing existing sections', () => {
  assert.equal(parseSystemSnapshot(good).opensearch.status, 'unavailable')
  const search = { status: 'online', cluster_health: 'green', node_count: 1,
    heap_used_bytes: 100, heap_max_bytes: 1024, index_store_bytes: 42 }
  const parsed = parseSystemSnapshot({ ...good, opensearch: search })
  assert.equal(parsed.opensearch.heapUsedBytes, 100)
  assert.equal(parsed.opensearch.clusterHealth, 'green')
  assert.equal(parsed.redis.queue.waiting, 2)
  assert.throws(() => parseSystemSnapshot({ ...good, opensearch: { ...search, heap_used_bytes: -1 } }))
  assert.match(monitor, /Metadata catalogue: document and run records are indexed/)
})

test('OpenSearch Docker container is accepted by monitor parser', () => {
  const parsed = parseSystemSnapshot({ ...good, docker: { status: 'online', collected_at: null, containers: [{
    service: 'opensearch', replica: 1, id: '123456789abc', state: 'running', health: 'healthy',
    memory_used_bytes: 0, memory_limit_bytes: 2147483648, cpu_percent: null, cpu_quota: null,
    cpuset_cpus: null, processes: null, threads: null, history: [],
  }] } })
  assert.equal(parsed.docker.containers[0].service, 'opensearch')
})

test('permanent archive aggregates are separate from temporary bucket and unknown values stay unknown', () => {
  const old = parseSystemSnapshot(good)
  assert.equal(old.archive.status, 'unavailable')
  assert.equal(old.archive.documents, null)
  const data = parseSystemSnapshot({...good, archive: {status:'online', truncated:true, checked_at:'2026-11-18T10:00:00Z',
    documents: 2, runs: 5, originals:2, bytes:10000, original_bytes:5000, artifact_bytes:4000, metadata_bytes:1000,
    indexed_documents: 2, indexed_runs: 5, run_status:{completed:4,partial:1}, run_cache:{miss:3,hit:1,shared:1},
    duplicate_submissions:2, reused_original_runs:null, consistency:'unknown', deletion_pending:false, reconciliation_needed:true,
    recovery:{state:'deferred',checked_at:null,recovered:null}}})
  assert.equal(data.archive.documents,2)
  assert.equal(data.archive.runStatus.completed,4)
  assert.equal(data.archive.runStatus.failed,null)
  assert.equal(data.archive.truncated,true)
  assert.equal(data.archive.reconciliationNeeded,true)
  assert.equal(data.archive.recovery.state,'deferred')
  assert.throws(()=>parseSystemSnapshot({...good,archive:{status:'online',documents:-1}}),/Invalid/)
  assert.throws(()=>parseSystemSnapshot({...good,archive:{status:'online',restore_pending:'yes'}}),/Invalid/)
  assert.match(monitor,/Permanent archive/)
  assert.match(monitor,/Catalogue consistency \(counts only\)/)
  assert.match(monitor,/validate_archive\.sh/)
})
