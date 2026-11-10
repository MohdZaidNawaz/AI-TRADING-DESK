export type InstrumentCategory = "forex" | "commodity" | "crypto"

export type Instrument = {
  instrument_id: string
  source_id: string
  category: InstrumentCategory
  provider_symbol: string
  display_symbol: string
  display_name: string | null
  base_currency: string | null
  quote_currency: string | null
  forex_group: string | null
  commodity_category: string | null
  description: string | null
  available_exchanges: string[]
  quote_access: "unknown"
}

export type CatalogueFilters = {
  query: string
  category: InstrumentCategory | ""
  forexGroup: string
  page: number
  limit: number
}

export type CatalogueResponse = {
  count: number
  page: number
  limit: number
  is_stale: boolean
  refreshed_at: string | null
  instruments: Instrument[]
}

export type InstrumentResponse = {
  is_stale: boolean
  refreshed_at: string | null
  instrument: Instrument
}

export const CATEGORY_LABELS: Record<InstrumentCategory, string> = {
  forex: "Forex",
  commodity: "Commodities",
  crypto: "Crypto",
}

export function cataloguePath(filters: CatalogueFilters): string {
  const params = new URLSearchParams({ page: String(filters.page), limit: String(filters.limit) })
  if (filters.query.trim()) params.set("q", filters.query.trim())
  if (filters.category) params.set("category", filters.category)
  if (filters.category === "forex" && filters.forexGroup.trim()) {
    params.set("forex_group", filters.forexGroup.trim())
  }
  return `/api/instruments?${params}`
}

export function cataloguePageCount(count: number, limit: number): number {
  return Math.max(1, Math.ceil(count / limit))
}

export function catalogueState(response: CatalogueResponse): "empty" | "stale" | "ready" {
  if (!response.instruments.length) return "empty"
  return response.is_stale ? "stale" : "ready"
}