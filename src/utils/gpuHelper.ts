import type { NodeStatusGpuDetail } from '@/utils/rpc'

export interface GpuSourceLike {
  gpu_name?: string | null
  gpu?: number | null
  gpu_detailed_info?: NodeStatusGpuDetail[] | null
}

const INTEGRATED_GPU_PREFIX_RE = /^Intel(?:\(R\))?\s+(?:UHD|HD|Iris|Integrated)\b/i
const TRAILING_MULTI_RE = /\s*[×x]\s*\d+$/i
const DRM_FALLBACK_RE = /^direct render manager\b/i

/**
 * 清洗静态 gpu_name：
 * - 过滤空值和 "None"
 * - 修正 Windows 下虚拟显示适配器导致的核显 "× N" 误报（核显不会多卡并联）
 */
export function sanitizeStaticGpuName(raw?: string | null): string {
  const trimmed = raw?.trim() ?? ''
  if (!trimmed || trimmed.toLowerCase() === 'none')
    return ''

  if (INTEGRATED_GPU_PREFIX_RE.test(trimmed))
    return trimmed.replace(TRAILING_MULTI_RE, '').trim()

  return trimmed
}

/**
 * 格式化展示用 GPU 型号：
 * 优先使用 agent 开启 --gpu 后上报的 gpu_detailed_info（真实物理卡，避免 Linux 的
 * "Direct Render Manager nvidia" 以及 Windows 虚拟屏导致的 "× 5" 误计数）。
 */
export function getGpuDisplayName(node?: GpuSourceLike | null): string {
  if (!node)
    return ''

  const details = (node.gpu_detailed_info ?? []).filter(g => g?.name?.trim() && g.name.trim().toLowerCase() !== 'none')
  if (details.length > 0) {
    const counts = new Map<string, number>()
    for (const g of details) {
      const name = g.name.trim()
      counts.set(name, (counts.get(name) ?? 0) + 1)
    }
    return Array.from(counts.entries())
      .map(([name, count]) => (count > 1 ? `${name} × ${count}` : name))
      .join(' + ')
  }

  return sanitizeStaticGpuName(node.gpu_name)
}

/**
 * 判断节点是否具备真实的实时 GPU 监控数据（用于决定是否展示 GPU 占用条与曲线，
 * 避免无独显/未开 --gpu 的核显节点常年显示一条 0.0% 占位进度条）。
 */
export function hasLiveGpuTelemetry(
  node?: GpuSourceLike | null,
  historyRecords?: Array<{ gpu?: number | null }> | null,
): boolean {
  if (!node)
    return false
  if ((node.gpu_detailed_info?.length ?? 0) > 0)
    return true
  if (typeof node.gpu === 'number' && node.gpu > 0)
    return true
  if (historyRecords?.some(r => typeof r.gpu === 'number' && r.gpu > 0))
    return true
  return false
}

/**
 * 是否值得在静态硬件信息栏展示 GPU（有真实详细 GPU，或有非 DRM 占位的静态名称）。
 */
export function hasHardwareGpuInfo(node?: GpuSourceLike | null): boolean {
  const name = getGpuDisplayName(node)
  if (!name)
    return false
  if ((node?.gpu_detailed_info?.length ?? 0) > 0)
    return true
  return !DRM_FALLBACK_RE.test(name)
}

export interface AggregateGpuMetrics {
  gpus: NodeStatusGpuDetail[]
  utilization: number
  memoryUsed: number
  memoryTotal: number
  temperature: number | null
}

export function getAggregateGpuMetrics(node?: GpuSourceLike | null): AggregateGpuMetrics {
  const gpus = node?.gpu_detailed_info ?? []
  if (gpus.length > 0) {
    const utilAvg = gpus.reduce((sum, g) => sum + (Number.isFinite(g.utilization) ? g.utilization : 0), 0) / gpus.length
    const memoryUsed = gpus.reduce((sum, g) => sum + (g.memory_used || 0), 0)
    const memoryTotal = gpus.reduce((sum, g) => sum + (g.memory_total || 0), 0)
    const temps = gpus.map(g => g.temperature).filter(t => typeof t === 'number' && t > 0)
    const temperature = temps.length > 0 ? Math.max(...temps) : null
    return {
      gpus,
      utilization: Math.min(100, Math.max(0, utilAvg)),
      memoryUsed,
      memoryTotal,
      temperature,
    }
  }

  const fallbackUtil = typeof node?.gpu === 'number' && Number.isFinite(node.gpu) ? node.gpu : 0
  return {
    gpus: [],
    utilization: Math.min(100, Math.max(0, fallbackUtil)),
    memoryUsed: 0,
    memoryTotal: 0,
    temperature: null,
  }
}
