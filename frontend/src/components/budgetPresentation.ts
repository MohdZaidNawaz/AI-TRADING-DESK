import type { LLMProviderPricingStatus, LLMUsageRecord } from "../services/api.ts"

export type ProviderUsageSummary = {
  key: string
  provider: LLMUsageRecord["provider"]
  model: string
  requestCount: number
  costInr: number
  estimatedOnlyCount: number
}

export function amount(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null
  const parsed = typeof value === "number" ? value : Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null
}

export function formatInr(value: string | number | null | undefined): string {
  const parsed = amount(value)
  if (parsed === null) return "Unknown"
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(parsed)
}

export function summarizeProviderUsage(
  records: LLMUsageRecord[],
): ProviderUsageSummary[] {
  const summaries = new Map<string, ProviderUsageSummary>()
  for (const record of records) {
    if (record.state !== "completed") continue
    const key = `${record.provider}:${record.model}`
    const existing = summaries.get(key) ?? {
      key,
      provider: record.provider,
      model: record.model,
      requestCount: 0,
      costInr: 0,
      estimatedOnlyCount: 0,
    }
    const actual = amount(record.actual_cost_inr)
    const estimated = amount(record.estimated_cost_inr)
    existing.requestCount += 1
    existing.costInr += actual ?? estimated ?? 0
    if (record.input_tokens === null || record.output_tokens === null) {
      existing.estimatedOnlyCount += 1
    }
    summaries.set(key, existing)
  }
  return [...summaries.values()].sort(
    (left, right) => right.costInr - left.costInr || left.key.localeCompare(right.key),
  )
}

export function providerHealthLabel(provider: LLMProviderPricingStatus): string {
  if (!provider.configured) return "Unavailable: not configured"
  if (provider.pricing_status === "unknown") return "Ineligible: pricing unknown"
  if (provider.paid_authorization_required) return "Paid authorization required"
  if (provider.health === "temporarily_unavailable") return "Temporarily unavailable"
  if (provider.health === "authentication_failed") return "Authentication failed"
  if (provider.health === "configuration_error") return "Configuration error"
  if (provider.tier === "free") return "Free tier eligible · quota unverified"
  return provider.eligible ? "Eligible" : "Unavailable"
}
