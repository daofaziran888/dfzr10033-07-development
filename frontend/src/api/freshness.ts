import { request } from './client'

export type StageRow = {
  环节: string
  判定: string
  时间: string
  耗时: number | null
  时限秒数: number
  原因: string
}

export type VerifyResult = {
  ok: boolean
  message?: string
  version: string
  check_start: string
  thresholds: Record<string, string>
  conclusion: string
  breached_stages: string[]
  missing_stages: string[]
  timeout_reasons: Record<string, string>
  new_alerts: string[]
  alert_counted: boolean
  counted_alerts: number
  duplicate: boolean
  requested_version_ignored: boolean
  requested_version_replayed: boolean
  breakdown: StageRow[]
}

export type RuleVersion = {
  版本: string
  生效时间: string
  说明: string
  阶段时限: Record<string, string>
  缺证据判定: Record<string, string>
}

export type SimulateResult = {
  ok: boolean
  version: string
  check_start: string
  arrival_orders: {
    顺序: string
    来源判定: string
    缓存判定: string
    超时原因: Record<string, string>
    结论: string
  }[]
  rule_boundary: {
    边界版本: string
    生效时间: string
    points: {
      核对点: string
      检查起点: string
      采用版本: string
      阶段时限: Record<string, string>
      结论: string
      违约环节: string[]
    }[]
  }
  sla_change: {
    适用: boolean
    对照口径: {
      口径: string
      检查起点: string
      采用版本: string
      阶段时限: Record<string, string>
      结论: string
      违约环节: string[]
    }[]
    登记建议: string
  }
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  return search.toString()
}

export async function loadRuleVersions(): Promise<RuleVersion[]> {
  const response = await request('/api/freshness/rules')
  if (!response.ok) throw new Error('分段时效规则读取失败')
  return (await response.json()).versions
}

export async function verifyBatch(params: {
  batch_id: string
  dataset: string
  check_start?: string
  rule_version?: string
  source_sync?: string
  lake_scan?: string
  cache_refresh?: string
}): Promise<VerifyResult> {
  const response = await request(`/api/freshness/verify?${query(params)}`, { method: 'POST' })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '分段时效核验失败')
  return payload
}

export async function simulateDelivery(params: {
  batch_id: string
  dataset: string
  check_start?: string
  rule_version?: string
}): Promise<SimulateResult> {
  const response = await request(`/api/freshness/simulate?${query(params)}`, { method: 'POST' })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '到达顺序模拟失败')
  return payload
}

export async function verifyContract(contractId: number): Promise<Record<string, unknown>> {
  const response = await request(`/api/freshness/contracts/${contractId}/verify`, { method: 'POST' })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '契约核验失败')
  return payload
}
