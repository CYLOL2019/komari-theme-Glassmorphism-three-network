import type { MaybeRefOrGetter } from 'vue'
import type { PingRecord, SharedPingRecordsState } from '@/composables/useNodePingStats'
import { computed, onScopeDispose, shallowRef, toValue, watch } from 'vue'
import { useSharedPingRecordsSnapshot } from '@/composables/useNodePingStats'
import { useAppStore } from '@/stores/app'
import { formatDateTime } from '@/utils/helper'

export type MainlandBlockStatusKind = 'ok' | 'tcp_blocked' | 'blocked' | 'icmp_only'

export interface MainlandProbeNodeEntry {
  icmp?: boolean | null
  tcp?: boolean | null
  port?: number | null
  icmp_ms?: number | null
  tcp_ms?: number | null
  status?: MainlandBlockStatusKind | string
  checked_at?: string
  probe_location?: string
}

export interface MainlandProbePayload {
  updated_at?: string
  probe_location?: string
  nodes?: Record<string, MainlandProbeNodeEntry>
}

export interface MainlandBlockDisplay {
  status: MainlandBlockStatusKind
  label: string
  icon: string
  badgeClass: string
  tooltip: string
  source: 'probe' | 'ping'
}

interface SharedProbeCacheEntry {
  data: ReturnType<typeof shallowRef<MainlandProbePayload | null>>
  promise: Promise<void> | null
  timer: ReturnType<typeof setInterval> | null
  subscribers: number
  lastFetchedAt: number
  intervalMs: number
}

const probeCache = new Map<string, SharedProbeCacheEntry>()

const TCP_TASK_PATTERN = /^(?:大陆|中国大陆|CN|GFW)[\s_-]*TCP[\s:_-]+([^\s:_-].*)$/i
const ICMP_TASK_PATTERN = /^(?:大陆|中国大陆|CN|GFW)[\s_-]*(?:ICMP|PING)[\s:_-]+([^\s:_-].*)$/i
const RECENT_SAMPLE_COUNT = 5

function normalizeKey(value: string): string {
  return value.trim().toLowerCase()
}

function getProbeCacheEntry(url: string, intervalMs: number): SharedProbeCacheEntry {
  const existing = probeCache.get(url)
  if (existing) {
    existing.intervalMs = intervalMs
    return existing
  }

  const created: SharedProbeCacheEntry = {
    data: shallowRef<MainlandProbePayload | null>(null),
    promise: null,
    timer: null,
    subscribers: 0,
    lastFetchedAt: 0,
    intervalMs,
  }
  probeCache.set(url, created)
  return created
}

async function fetchProbePayload(url: string, entry: SharedProbeCacheEntry): Promise<void> {
  if (entry.promise)
    return entry.promise

  entry.promise = (async () => {
    try {
      const separator = url.includes('?') ? '&' : '?'
      const response = await fetch(`${url}${separator}_t=${Date.now()}`, {
        cache: 'no-store',
      })
      if (!response.ok)
        return

      const parsed = await response.json() as unknown
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        entry.data.value = parsed as MainlandProbePayload
        entry.lastFetchedAt = Date.now()
      }
    }
    catch {
    }
    finally {
      entry.promise = null
    }
  })()

  return entry.promise
}

function retainProbeUrl(url: string, intervalMs: number): () => void {
  const entry = getProbeCacheEntry(url, intervalMs)
  entry.subscribers += 1

  if (!entry.timer) {
    entry.timer = setInterval(() => {
      void fetchProbePayload(url, entry)
    }, Math.max(15_000, entry.intervalMs))
  }

  if (!entry.data.value || Date.now() - entry.lastFetchedAt >= entry.intervalMs) {
    void fetchProbePayload(url, entry)
  }

  let released = false
  return () => {
    if (released)
      return
    released = true
    entry.subscribers = Math.max(0, entry.subscribers - 1)
    if (entry.subscribers === 0 && entry.timer) {
      clearInterval(entry.timer)
      entry.timer = null
    }
  }
}

function resolveStatusKind(entry: MainlandProbeNodeEntry): MainlandBlockStatusKind | null {
  if (
    entry.status === 'ok'
    || entry.status === 'tcp_blocked'
    || entry.status === 'blocked'
    || entry.status === 'icmp_only'
  ) {
    return entry.status
  }

  const hasTcp = typeof entry.tcp === 'boolean'
  const hasIcmp = typeof entry.icmp === 'boolean'
  if (!hasTcp && !hasIcmp)
    return null

  if (hasTcp && entry.tcp)
    return 'ok'
  if (hasTcp && !entry.tcp && hasIcmp && entry.icmp)
    return 'tcp_blocked'
  if (hasTcp && !entry.tcp)
    return 'blocked'
  if (hasIcmp && entry.icmp)
    return 'icmp_only'
  return 'blocked'
}

function findProbeNodeEntry(
  payload: MainlandProbePayload | null,
  uuid: string,
  name: string,
): MainlandProbeNodeEntry | null {
  if (!payload?.nodes || typeof payload.nodes !== 'object')
    return null

  if (uuid && payload.nodes[uuid])
    return payload.nodes[uuid]

  if (name && payload.nodes[name])
    return payload.nodes[name]

  const targetName = normalizeKey(name)
  const targetUuid = normalizeKey(uuid)
  for (const [key, value] of Object.entries(payload.nodes)) {
    const normalized = normalizeKey(key)
    if ((targetUuid && normalized === targetUuid) || (targetName && normalized === targetName))
      return value
  }
  return null
}

function summarizeRecentRecords(records: PingRecord[]): { reachable: boolean, avgMs: number | null, lossPct: number } | null {
  if (!records.length)
    return null

  const sorted = [...records].sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime())
  const recent = sorted.slice(-RECENT_SAMPLE_COUNT)
  const okValues = recent.map(item => item.value).filter(val => val >= 0)
  const lossPct = ((recent.length - okValues.length) / recent.length) * 100
  const avgMs = okValues.length
    ? Math.round(okValues.reduce((sum, val) => sum + val, 0) / okValues.length)
    : null

  return {
    reachable: okValues.length > 0 && lossPct < 80,
    avgMs,
    lossPct,
  }
}

function deriveFromNativePing(
  snapshot: SharedPingRecordsState | null,
  nodeName: string,
): MainlandProbeNodeEntry | null {
  if (!snapshot || !nodeName.trim())
    return null

  const normalizedNodeName = normalizeKey(nodeName)
  const tcpTaskIds = new Set<number>()
  const icmpTaskIds = new Set<number>()

  for (const task of snapshot.tasks) {
    const tcpMatch = task.name.trim().match(TCP_TASK_PATTERN)
    if (tcpMatch?.[1] && normalizeKey(tcpMatch[1]) === normalizedNodeName) {
      tcpTaskIds.add(task.id)
      continue
    }
    const icmpMatch = task.name.trim().match(ICMP_TASK_PATTERN)
    if (icmpMatch?.[1] && normalizeKey(icmpMatch[1]) === normalizedNodeName) {
      icmpTaskIds.add(task.id)
    }
  }

  if (!tcpTaskIds.size && !icmpTaskIds.size)
    return null

  const tcpRecords: PingRecord[] = []
  const icmpRecords: PingRecord[] = []
  for (const records of snapshot.recordsByClient.values()) {
    for (const record of records) {
      if (tcpTaskIds.has(record.task_id))
        tcpRecords.push(record)
      else if (icmpTaskIds.has(record.task_id))
        icmpRecords.push(record)
    }
  }

  const tcpSummary = summarizeRecentRecords(tcpRecords)
  const icmpSummary = summarizeRecentRecords(icmpRecords)
  if (!tcpSummary && !icmpSummary)
    return null

  const latestTime = [...tcpRecords, ...icmpRecords]
    .map(item => item.time)
    .sort()
    .at(-1)

  return {
    tcp: tcpSummary ? tcpSummary.reachable : null,
    tcp_ms: tcpSummary?.avgMs ?? null,
    icmp: icmpSummary ? icmpSummary.reachable : null,
    icmp_ms: icmpSummary?.avgMs ?? null,
    checked_at: latestTime,
  }
}

function formatDisplay(
  entry: MainlandProbeNodeEntry,
  source: 'probe' | 'ping',
  lang: 'zh-CN' | 'en-US',
  fallbackLocation?: string,
  fallbackUpdatedAt?: string,
): MainlandBlockDisplay | null {
  const status = resolveStatusKind(entry)
  if (!status)
    return null

  const isZh = lang === 'zh-CN'
  const portLabel = entry.port ? `:${entry.port}` : ''
  const tcpText = typeof entry.tcp === 'boolean'
    ? entry.tcp
      ? `${isZh ? 'TCP' : 'TCP'}${portLabel} ${isZh ? '通' : 'OK'}${typeof entry.tcp_ms === 'number' ? ` (${Math.round(entry.tcp_ms)} ms)` : ''}`
      : `${isZh ? 'TCP' : 'TCP'}${portLabel} ${isZh ? '阻断/超时' : 'Blocked/Timeout'}`
    : null
  const icmpText = typeof entry.icmp === 'boolean'
    ? entry.icmp
      ? `ICMP ${isZh ? '通' : 'OK'}${typeof entry.icmp_ms === 'number' ? ` (${Math.round(entry.icmp_ms)} ms)` : ''}`
      : `ICMP ${isZh ? '不通' : 'Blocked'}`
    : null

  const location = entry.probe_location || fallbackLocation
  const checkedAt = entry.checked_at || fallbackUpdatedAt
  const detailLines = [
    isZh ? '中国大陆直连检测' : 'China Mainland Direct Check',
    location ? `${isZh ? '探测点' : 'Probe'}: ${location}` : null,
    tcpText,
    icmpText,
    checkedAt ? `${isZh ? '更新' : 'Checked'}: ${formatDateTime(checkedAt, 'HH:mm:ss')}` : null,
  ].filter(Boolean)

  if (status === 'ok') {
    return {
      status,
      label: isZh ? '大陆直连' : 'CN OK',
      icon: 'tabler:shield-check',
      badgeClass: 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400 border border-emerald-500/25',
      tooltip: detailLines.join('\n'),
      source,
    }
  }

  if (status === 'tcp_blocked') {
    return {
      status,
      label: isZh ? 'TCP阻断' : 'CN TCP Blocked',
      icon: 'tabler:shield-exclamation',
      badgeClass: 'bg-amber-500/15 text-amber-700 dark:text-amber-400 border border-amber-500/30',
      tooltip: detailLines.join('\n'),
      source,
    }
  }

  if (status === 'icmp_only') {
    return {
      status,
      label: isZh ? 'ICMP可达' : 'CN ICMP OK',
      icon: 'tabler:shield-half',
      badgeClass: 'bg-sky-500/12 text-sky-600 dark:text-sky-400 border border-sky-500/25',
      tooltip: detailLines.join('\n'),
      source,
    }
  }

  return {
    status: 'blocked',
    label: isZh ? '疑似被墙' : 'CN Blocked',
    icon: 'tabler:shield-x',
    badgeClass: 'bg-rose-500/15 text-rose-600 dark:text-rose-400 border border-rose-500/30',
    tooltip: detailLines.join('\n'),
    source,
  }
}

/**
 * 获取节点对中国大陆的直连连通性 / 封锁状态。
 * 1) 若在主题后台配置了 `mainlandBlockProbeUrl`，优先读取该探针 JSON；
 * 2) 否则自动回退至 Komari 原生 Ping 任务命名匹配（如 `大陆TCP:节点名` / `大陆ICMP:节点名`）。
 */
export function useNodeMainlandBlockStatus(
  uuid: MaybeRefOrGetter<string>,
  name: MaybeRefOrGetter<string>,
) {
  const appStore = useAppStore()
  const enabled = computed(() => appStore.mainlandBlockCheckEnabled)
  const probeUrl = computed(() => appStore.mainlandBlockProbeUrl)
  const refreshMs = computed(() => appStore.mainlandBlockRefreshSeconds * 1000)

  let activeUrl: string | null = null
  let releaseProbe: (() => void) | null = null

  function syncProbeSubscription(url: string | null, intervalMs: number): void {
    if (activeUrl === url)
      return

    releaseProbe?.()
    releaseProbe = null
    activeUrl = null

    if (!url)
      return

    releaseProbe = retainProbeUrl(url, intervalMs)
    activeUrl = url
  }

  watch(
    [enabled, probeUrl, refreshMs],
    ([isEnabled, url, intervalMs]) => {
      syncProbeSubscription(isEnabled && url ? url : null, intervalMs)
    },
    { immediate: true },
  )

  onScopeDispose(() => {
    syncProbeSubscription(null, 60_000)
  })

  const pingEnabled = computed(() => enabled.value && appStore.publicSettings?.record_enabled !== false)
  const { snapshot: pingSnapshot } = useSharedPingRecordsSnapshot({
    hours: 1,
    enabled: pingEnabled,
  })

  const display = computed<MainlandBlockDisplay | null>(() => {
    if (!enabled.value)
      return null

    const nodeUuid = toValue(uuid)
    const nodeName = toValue(name)
    const url = probeUrl.value

    if (url) {
      const payload = getProbeCacheEntry(url, refreshMs.value).data.value ?? null
      const matched = findProbeNodeEntry(payload, nodeUuid, nodeName)
      if (matched) {
        return formatDisplay(matched, 'probe', appStore.lang, payload?.probe_location, payload?.updated_at)
      }
    }

    const nativeEntry = deriveFromNativePing(pingSnapshot.value, nodeName)
    if (nativeEntry) {
      return formatDisplay(nativeEntry, 'ping', appStore.lang)
    }

    return null
  })

  return {
    display,
  }
}
