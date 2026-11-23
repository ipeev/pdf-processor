import { useEffect, useState } from 'react'
import {
  parseSystemSnapshot, compactBytes, countLabel, usd,
  type SystemSnapshot, type WorkerTelemetry, type DockerContainer,
} from './system'

const REFRESH_MS = 3000

type Health = 'online' | 'unavailable' | 'not_monitored'
function HealthTag({ status, text }: { status: Health; text?: string }) {
  return <span className={`monitor-health ${status}`}><i aria-hidden="true"/>{text ?? (status === 'online' ? 'ONLINE' : status === 'unavailable' ? 'UNAVAILABLE' : 'NOT PROBED')}</span>
}
function MiniIcon({ kind }: { kind: 'api' | 'redis' | 'storage' | 'worker' | 'cloud' }) {
  const shapes = {
    api: <><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m8 10-2 2 2 2m8-4 2 2-2 2M13 9l-2 6"/></>,
    redis: <><path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 17l9 5 9-5"/></>,
    storage: <><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v13c0 4 18 4 18 0V5M3 12c0 4 18 4 18 0"/></>,
    worker: <><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h6v6H9zM9 1v3m6-3v3M9 20v3m6-3v3M1 9h3m-3 6h3m16-6h3m-3 6h3"/></>,
    cloud: <><path d="M7 19a5 5 0 0 1-1-9.9A7 7 0 0 1 19 11a4 4 0 0 1-1 8H7Z"/><path d="m10 14 2-2 2 2m-2-2v6"/></>,
  }
  return <svg aria-hidden="true" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{shapes[kind]}</svg>
}
function Node({ kind, title, caption, health, active, extra }: {
  kind: 'api' | 'redis' | 'storage' | 'worker' | 'cloud'
  title: string; caption: string; health: Health; active?: boolean; extra?: string
}) {
  return <div className={`flow-node ${health} ${active ? 'is-active' : ''}`}>
    <div className="flow-node-heading"><span className={`flow-node-icon ${kind}`}><MiniIcon kind={kind}/></span><HealthTag status={health}/></div>
    <strong>{title}</strong><span className="flow-node-caption">{caption}</span>
    {extra && <span className="flow-node-detail">{extra}</span>}
  </div>
}
function MetricCard({ title, value, note, accent }: { title: string; value: string; note: string; accent?: string }) {
  return <div className="monitor-metric"><div className="monitor-metric-title">{title}</div><strong className={accent ?? ''}>{value}</strong><span>{note}</span></div>
}
function WorkerCard({ group, index }: { group: WorkerGroup; index: number }) {
  return <div className={`worker-card ${group.busy ? 'busy' : ''}`}>
    <span className="worker-avatar"><MiniIcon kind="worker"/></span>
    <div><strong>Container {index + 1}</strong><small>{group.workers.length} live thread(s) · {group.id}</small></div>
    <div className="worker-current"><span className={`worker-state ${group.busy ? 'extracting' : 'idle'}`}>{group.busy} active / {group.workers.length} live</span>
      <small>{group.summarizing} summarizing · {group.extracting} extracting</small></div>
  </div>
}

type WorkerGroup = { id: string; workers: WorkerTelemetry[]; busy: number; extracting: number; summarizing: number }
function groupWorkers(workers: WorkerTelemetry[]): WorkerGroup[] {
  const groups = new Map<string, WorkerTelemetry[]>()
  for (const worker of workers) {
    const id = worker.container ?? 'legacy'
    groups.set(id, [...(groups.get(id) ?? []), worker])
  }
  return Array.from(groups, ([id, members]) => ({
    id, workers: members,
    busy: members.filter(worker => worker.state !== 'idle').length,
    extracting: members.filter(worker => worker.state === 'extracting').length,
    summarizing: members.filter(worker => worker.state === 'summarizing').length,
  })).sort((a, b) => a.id.localeCompare(b.id))
}

type KeyView = SystemSnapshot['openrouter'] | undefined
function OpenRouterNode({ data }: { data: KeyView }) {
  const ok = data?.status === 'available'
  const label = ok ? 'KEY DATA OK' : data?.status === 'not_configured' ? 'NO KEY' :
    data?.status === 'auth_error' ? 'KEY REJECTED' : data?.status === 'rate_limited' ? 'RATE LIMITED' : data?.status === 'not_monitored' ? 'KEY CHECK DISABLED' : 'UNAVAILABLE'
  return <div className={`flow-node openrouter-topology ${ok ? 'online' : ''}`} aria-label="OpenRouter key usage">
    <div className="flow-node-heading"><span className="flow-node-icon cloud"><MiniIcon kind="cloud"/></span>
      <HealthTag status={ok ? 'online' : data?.status === 'not_configured' || data?.status === 'not_monitored' ? 'not_monitored' : 'unavailable'} text={label}/></div>
    <strong>OpenRouter key usage</strong>
    <span className="flow-node-caption">Monthly: {usd(data?.usageMonthlyUsd ?? null)} · Today: {usd(data?.usageDailyUsd ?? null)}</span>
    <span className="flow-node-caption">Total: {usd(data?.usageUsd ?? null)} · 7 days: {usd(data?.usageWeeklyUsd ?? null)}</span>
    <span className="flow-node-caption">Key allowance remaining: {data?.keyLimitRemainingUsd == null ? 'Not reported' : usd(data.keyLimitRemainingUsd)}</span>
    <span className="flow-node-caption">Key limit: {data?.keyLimitUsd == null ? 'Not reported' : usd(data.keyLimitUsd)}</span>
    <span className="flow-node-caption">Budget headroom: {data?.budgetRemainingUsd == null ? 'Not reported' : `${usd(data.budgetRemainingUsd)} (${data.budgetScope ?? 'unknown scope'})`}</span>
    <span className="flow-node-detail">{data?.checkedAt ? `Checked ${new Date(data.checkedAt).toLocaleTimeString()}` : 'No key metadata'} · Inference not probed</span>
  </div>
}
function CpuHistory({ points }: { points: DockerContainer['history'] }) {
  const readings = points.filter(point => point.cpuPercent !== null)
  if (!readings.length) return <div className="cpu-history-empty">No CPU history yet</div>
  const end = Date.parse(readings[readings.length - 1].timestamp)
  const start = end - 300_000
  const peak = Math.max(100, ...readings.map(point => point.cpuPercent ?? 0))
  const coords = readings.map(point => {
    const x = Math.max(0, Math.min(300, (Date.parse(point.timestamp) - start) / 300_000 * 300))
    const y = 69 - (point.cpuPercent ?? 0) / peak * 62
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const average = readings.reduce((total, point) => total + (point.cpuPercent ?? 0), 0) / readings.length
  return <div className="cpu-history"><svg viewBox="0 0 300 76" role="img" aria-label={`CPU usage over the last five minutes; average ${average.toFixed(1)} percent`} preserveAspectRatio="none">
    <line x1="0" y1="69" x2="300" y2="69" stroke="#39526a"/>
    <polyline fill="none" stroke="#78e2ca" strokeWidth="2.5" strokeLinejoin="round" points={coords}/>
  </svg><small>5m avg {average.toFixed(1)}% · max {peak === 100 ? Math.max(...readings.map(point => point.cpuPercent ?? 0)).toFixed(1) : peak.toFixed(1)}% · {readings.length} samples</small></div>
}
function DockerCard({ container }: { container: DockerContainer }) {
  const ram = container.memoryUsedBytes
  const limit = container.memoryLimitBytes
  const load = limit && ram != null ? `${Math.round(ram / limit * 100)}%` : '—'
  return <article className="docker-resource-card">
    <div className="docker-resource-head"><strong>{container.service === 'worker' ? `Worker ${container.replica}` : container.service}</strong>
      <span className={`monitor-health ${container.state === 'running' ? 'online' : 'unavailable'}`}>{container.health !== 'none' ? container.health : container.state}</span></div>
    <div className="docker-resource-key">CPU now <strong>{container.cpuPercent == null ? '—' : `${container.cpuPercent.toFixed(1)}%`}</strong></div>
    <CpuHistory points={container.history}/>
    <div className="docker-resource-grid">
      <div>RAM used<strong>{compactBytes(ram)}</strong></div>
      <div>RAM limit<strong>{limit == null ? 'No explicit limit' : compactBytes(limit)}</strong></div>
      <div>RAM % of limit<strong>{load}</strong></div>
      <div>CPU quota<strong>{container.cpuQuota == null ? 'No explicit quota' : `${container.cpuQuota.toFixed(2)} CPUs`}</strong></div>
      <div>Allowed CPU set<strong>{container.cpusetCpus == null ? 'Not restricted' : `${container.cpusetCpus} CPUs`}</strong></div>
      <div>Processes / threads<strong>{container.processes ?? '—'} / {container.threads ?? '—'}</strong></div>
    </div>
  </article>
}

export default function SystemMonitor() {
  const [snapshot, setSnapshot] = useState<SystemSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [updated, setUpdated] = useState<Date | null>(null)
  useEffect(() => {
    let active = true
    const controller = new AbortController()
    let busy = false
    async function refresh() {
      if (busy) return
      busy = true
      try {
        const response = await fetch('/api/v1/system/status', { cache: 'no-store', signal: controller.signal })
        if (!response.ok) throw new Error(`Monitoring API returned HTTP ${response.status}`)
        const parsed = parseSystemSnapshot(await response.json())
        if (active) { setSnapshot(parsed); setError(null); setUpdated(new Date()) }
      } catch (failure) {
        if (active) setError(failure instanceof Error ? failure.message : 'Monitoring API unavailable')
      } finally { busy = false }
    }
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, REFRESH_MS)
    return () => { active = false; controller.abort(); window.clearInterval(timer) }
  }, [])
  const redis = snapshot?.redis
  const rustfs = snapshot?.rustfs
  const key = snapshot?.openrouter
  const search = snapshot?.opensearch
  const archive = snapshot?.archive
  const archiveCount = (n: number | null | undefined) => n == null ? '—' : `${archive?.truncated ? '≥' : ''}${countLabel(n)}`
  const archiveBytes = (n: number | null | undefined) => n == null ? '—' : `${archive?.truncated ? '≥' : ''}${compactBytes(n)}`
  const flag = (v: boolean | null | undefined) => v == null ? '—' : v ? 'Yes' : 'No'
  const consistency = { unknown: 'Unknown', attention: 'Maintenance pending', counts_match: 'Counts match', count_mismatch: 'Count mismatch' } as const
  const workers = redis?.workers ?? []
  const busyWorkers = workers.filter(worker => worker.state !== 'idle').length
  const groups = groupWorkers(workers)
  const [first, second] = [groups[0] ?? null, groups[1] ?? null]
  const live = snapshot !== null && !error
  const cache = redis?.cache
  const totalCache = (cache?.hits ?? 0) + (cache?.misses ?? 0) + (cache?.shared ?? 0)
  const cacheRate = totalCache > 0 && cache?.hits !== null && cache?.hits !== undefined ? `${Math.round(cache.hits / totalCache * 100)}%` : '—'
  const usedPct = redis?.memoryBytes != null && redis.memoryLimitBytes != null && redis.memoryLimitBytes > 0
    ? Math.min(100, Math.round(redis.memoryBytes / redis.memoryLimitBytes * 100)) : 0

  return <main className="container main-content monitor-main">
    <section className="monitor-hero">
      <div><div className="eyebrow"><span className="eyebrow-line"/> LIVE INFRASTRUCTURE</div>
        <h1>Inside the <span className="gradient-text">pipeline.</span></h1>
        <p>Real-time state from Redis, RustFS, OpenSearch, OpenRouter key metadata and host-collected Docker resource statistics.</p></div>
      <div className="monitor-refresh"><span className={`monitor-live-dot ${live ? 'live' : ''}`}/>
        <span>{live ? 'LIVE TELEMETRY' : 'WAITING FOR TELEMETRY'}<small>{updated ? `Last update ${updated.toLocaleTimeString()}` : 'Connecting to monitoring API'} · 3s refresh</small></span></div>
    </section>
    {error && <div role="alert" className="notice warning monitor-error">Monitoring update unavailable: {error}. {snapshot ? 'Showing the last successful snapshot.' : 'Live measurements will appear when the API responds.'}</div>}
    <section className="monitor-overview" aria-label="System overview">
      <MetricCard title="QUEUE · WAITING" value={countLabel(redis?.queue.waiting ?? null)} note="Redis consumer-group lag"/>
      <MetricCard title="JOBS · IN FLIGHT" value={countLabel(redis?.queue.pending ?? null)} note="Pending Redis messages"/>
      <MetricCard title="WORKERS · ACTIVE" value={redis?.status === 'online' ? `${busyWorkers} / ${workers.length}` : '—'} note="Live worker heartbeats" accent="teal-text"/>
      <MetricCard title="PDFS IN S3" value={rustfs?.objects === null || rustfs?.objects === undefined ? '—' : `${rustfs.truncated ? '≥' : ''}${countLabel(rustfs.objects)}`} note="Temporary source objects"/>
    </section>

    <section className="panel monitor-architecture" aria-label="System architecture">
      <div className="monitor-section-header"><div><span className="panel-overline">01 / LIVE TOPOLOGY</span><h2>Service architecture</h2><p>Connections show system design; worker badges and service metrics show observed state.</p></div><span className="topology-tag">7 COMPONENTS</span></div>
      <div className="flow-diagram">
        <div className="flow-tier flow-tier-top">
          <Node kind="api" title="FastAPI" caption="Upload & orchestration" health={snapshot ? 'online' : 'unavailable'} extra="HTTP · :8000"/>
          <Node kind="redis" title="Redis Streams" caption="Job queue & cache" health={redis?.status ?? 'unavailable'} extra={redis?.status === 'online' ? `${countLabel(redis.keyCount)} keys` : 'No connection'}/>
          <Node kind="storage" title="RustFS · S3" caption="Temporary + permanent buckets" health={rustfs?.status ?? 'unavailable'} extra={archive?.status === 'online' ? archiveBytes(archive.bytes) + ' archived' : 'Archive metrics unavailable'}/>
          <Node kind="storage" title="OpenSearch" caption="Read-only monitoring · Stage 1" health={search?.status ?? 'unavailable'} extra={search?.clusterHealth ?? 'No connection'}/>
        </div>
        <div className="flow-downlinks"><span/>API → SEARCH HEALTH · API → QUEUE + S3 · WORKERS ← QUEUE + S3 · WORKERS → AI<span/></div>
        <div className="flow-tier flow-tier-bottom">
          <Node kind="worker" title="Container 1" caption={first ? `${first.busy} active / ${first.workers.length} live` : 'No live heartbeat'} health={first ? 'online' : 'unavailable'} active={!!first?.busy} extra={first ? `${first.summarizing} summarizing` : 'Not detected'}/>
          <Node kind="worker" title="Container 2" caption={second ? `${second.busy} active / ${second.workers.length} live` : 'No live heartbeat'} health={second ? 'online' : 'unavailable'} active={!!second?.busy} extra={second ? `${second.summarizing} summarizing` : 'Not detected'}/>
          <OpenRouterNode data={key}/>
        </div>
      </div>
      <div className="topology-caption"><span className="legend-dot green"/> Online <span className="legend-dot purple"/> Processing
        <span className="legend-dot gray"/> Not monitored / unavailable
        <span className="monitor-disclaimer">Active worker stages are observed; arrows are illustrative, not distributed traces.</span></div>
    </section>

    <div className="monitor-details">
      <section className="panel monitor-data-card"><div className="monitor-section-header"><div><span className="panel-overline">02 / INFRASTRUCTURE</span><h2>Redis & queue</h2></div><HealthTag status={redis?.status ?? 'unavailable'}/></div>
        <div className="memory-header"><span>Memory utilization</span><strong>{compactBytes(redis?.memoryBytes ?? null)} <em>/ {compactBytes(redis?.memoryLimitBytes ?? null)}</em></strong></div>
        <div className="memory-track"><div style={{ width: `${usedPct}%` }}/></div>
        <div className="monitor-data-rows"><div><span>Redis keys (all types)</span><strong>{countLabel(redis?.keyCount ?? null)}</strong></div>
          <div><span>Undelivered stream jobs</span><strong>{countLabel(redis?.queue.waiting ?? null)}</strong></div>
          <div><span>Pending stream messages</span><strong>{countLabel(redis?.queue.pending ?? null)}</strong></div></div>
        <p className="monitor-footnote">Waiting is consumer-group lag, not a total document count. Redis has a 1.5 GiB data cap and no eviction.</p>
      </section>
      <section className="panel monitor-data-card"><div className="monitor-section-header"><div><span className="panel-overline">03 / OBJECT STORAGE</span><h2>RustFS bucket</h2></div><HealthTag status={rustfs?.status ?? 'unavailable'}/></div>
        <div className="storage-stat"><span><MiniIcon kind="storage"/></span><strong>{rustfs?.objects == null ? '—' : `${rustfs.truncated ? '≥' : ''}${countLabel(rustfs.objects)}`}<small>temporary PDF objects</small></strong></div>
        <div className="monitor-data-rows"><div><span>Stored source-PDF bytes</span><strong>{compactBytes(rustfs?.bytes ?? null)}</strong></div>
          <div><span>Cleanup</span><strong>After ACK + lifecycle</strong></div>
          <div><span>Storage type</span><strong>Disk-backed S3</strong></div></div>
        <p className="monitor-footnote">Counts come from S3 ListObjectsV2, not Redis. {rustfs?.truncated ? 'Large bucket: showing a bounded count.' : 'Original files are removed after acknowledged processing.'}</p>
      </section>
      <section className="panel monitor-data-card"><div className="monitor-section-header"><div><span className="panel-overline">04 / DEDUPLICATION</span><h2>SHA-256 cache</h2></div><HealthTag status={redis?.status ?? 'unavailable'}/></div>
        <div className="cache-summary"><strong>{cacheRate}</strong><span>Completed-result hit ratio<br/><small>since Redis counters started</small></span></div>
        <div className="monitor-data-rows"><div><span>Cache hits</span><strong>{countLabel(cache?.hits ?? null)}</strong></div>
          <div><span>New jobs (misses)</span><strong>{countLabel(cache?.misses ?? null)}</strong></div>
          <div><span>Shared in-flight jobs</span><strong>{countLabel(cache?.shared ?? null)}</strong></div></div>
        <p className="monitor-footnote">Upload outcomes only. Counters reset with Redis; they are not Redis keyspace hit statistics.</p>
      </section>
      <section className="panel monitor-data-card"><div className="monitor-section-header"><div><span className="panel-overline">05 / EXECUTION</span><h2>Python workers</h2></div><span className="topology-tag">{workers.length} LIVE</span></div>
        <div className="worker-list">{groups.map((group, index) => <WorkerCard key={group.id} group={group} index={index}/>)}</div>
        <p className="monitor-footnote">Each worker thread publishes an expiring heartbeat. Container grouping uses an opaque ID; counts reflect live instances, not configured capacity.</p>
      </section>
    </div>
    <section className="panel monitor-data-card" aria-label="OpenSearch infrastructure"><div className="monitor-section-header"><div><span className="panel-overline">06 / SEARCH INFRASTRUCTURE</span><h2>OpenSearch</h2></div><HealthTag status={search?.status ?? 'unavailable'}/></div>
      <div className="monitor-data-rows"><div><span>Cluster health</span><strong>{search?.clusterHealth ?? '—'}</strong></div>
        <div><span>Nodes</span><strong>{countLabel(search?.nodeCount ?? null)}</strong></div>
        <div><span>JVM heap used / max</span><strong>{compactBytes(search?.heapUsedBytes ?? null)} / {compactBytes(search?.heapMaxBytes ?? null)}</strong></div>
        <div><span>Index storage (cluster total)</span><strong>{compactBytes(search?.indexStoreBytes ?? null)}</strong></div></div>
      <p className="monitor-footnote">Metadata catalogue: document and run records are indexed; PDF contents and result bodies remain in RustFS.</p>
    </section>
    <section className="panel monitor-data-card" aria-label="Permanent archive statistics">
      <div className="monitor-section-header"><div><span className="panel-overline">07 / PERMANENT STORAGE</span><h2>Permanent archive</h2><p>RustFS object inventory and OpenSearch catalogue aggregates.</p></div><HealthTag status={archive?.status ?? 'unavailable'}/></div>
      <div className="monitor-overview">
        <MetricCard title="UNIQUE DOCUMENTS" value={archiveCount(archive?.documents)} note="Canonical manifests"/>
        <MetricCard title="PROCESSING RUNS" value={archiveCount(archive?.runs)} note="Permanent run manifests"/>
        <MetricCard title="ARCHIVE BYTES" value={archiveBytes(archive?.bytes)} note="Logical object size"/>
        <MetricCard title="CACHE REUSES" value={countLabel(archive?.duplicateSubmissions ?? null)} note="Indexed hits + shared runs"/>
      </div>
      <div className="monitor-data-rows">
        <div><span>Original PDFs / bytes</span><strong>{archiveCount(archive?.originals)} / {archiveBytes(archive?.originalBytes)}</strong></div>
        <div><span>Result artifacts (all generations)</span><strong>{archiveBytes(archive?.artifactBytes)}</strong></div>
        <div><span>Metadata / other objects</span><strong>{archiveBytes(archive?.metadataBytes)}</strong></div>
        <div><span>Stored objects</span><strong>{archiveCount(archive?.objects)}</strong></div>
        <div><span>Indexed documents / runs</span><strong>{countLabel(archive?.indexedDocuments ?? null)} / {countLabel(archive?.indexedRuns ?? null)}</strong></div>
        <div><span>Indexed run states</span><strong>{archive ? Object.entries(archive.runStatus).map(([state, n]) => `${state}: ${countLabel(n)}`).join(' · ') : '—'}</strong></div>
        <div><span>Additional retained runs sharing originals</span><strong>{countLabel(archive?.reusedOriginalRuns ?? null)}</strong></div>
        <div><span>Indexed cache outcomes</span><strong>{archive ? Object.entries(archive.runCache).map(([state, n]) => `${state}: ${countLabel(n)}`).join(' · ') : '—'}</strong></div>
        <div><span>Catalogue consistency (counts only)</span><strong>{consistency[archive?.consistency ?? 'unknown']}</strong></div>
        <div><span>Deletion / reconciliation / restore pending</span><strong>{flag(archive?.deletionPending)} / {flag(archive?.reconciliationNeeded)} / {flag(archive?.restorePending)}</strong></div>
        <div><span>Last automatic recovery attempt</span><strong>{archive?.recovery.state ?? 'unknown'} · {archive?.recovery.checkedAt ? new Date(archive.recovery.checkedAt).toLocaleString() : '—'}</strong></div>
        <div><span>Runs recovered in last successful attempt</span><strong>{countLabel(archive?.recovery.recovered ?? null)}</strong></div>
      </div>
      <p className="monitor-footnote">Sample: {archive?.checkedAt ? new Date(archive.checkedAt).toLocaleString() : 'unavailable'}. Cached for 30 seconds; ≥ denotes a bounded scan. Live writes may cause transient count differences. Counts do not verify object integrity. For full checks use <code>./scripts/validate_archive.sh</code> while idle. Cache reuse does not measure physical storage saved.</p>
    </section>
    <section className="panel monitor-docker" aria-label="Docker resource telemetry">
      <div className="monitor-section-header"><div><span className="panel-overline">08 / HOST RESOURCE TELEMETRY</span>
        <h2>Docker containers</h2><p>Five-minute history sampled every five seconds by the local host collector.</p></div>
        <HealthTag status={snapshot?.docker.status ?? 'unavailable'} text={snapshot?.docker.status === 'online' ? 'COLLECTOR LIVE' : 'TELEMETRY UNAVAILABLE'}/></div>
      {snapshot?.docker.status === 'online' && snapshot.docker.containers.length ?
        <div className="docker-resource-list">{snapshot.docker.containers.map(container => <DockerCard key={container.id} container={container}/>)}</div> :
        <p className="monitor-footnote">Docker resource telemetry is unavailable. Run <code>./scripts/telemetry.sh start</code> on the Ubuntu host after setup. This does not affect PDF processing.</p>}
      <p className="monitor-footnote">CPU: 100% represents one logical CPU (values may exceed 100%). RAM uses Docker's working-set statistics. Limits reflect Docker constraints, not resource reservations. CPU-set counts represent permitted CPUs, not dedicated allocations. Process and thread counts come from Docker top; unavailable values are shown as —.</p>
    </section>
    <div className="architecture-note"><MiniIcon kind="cloud"/><span><strong>Local-only system view.</strong> OpenRouter inference health is not probed. Key metadata excludes credentials and document contents; training/retention settings are not verified by the key endpoint.</span></div>
  </main>
}
