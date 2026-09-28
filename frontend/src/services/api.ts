const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/$/, "")

export type ApiConnectionState = "loading" | "online" | "error"

export type Agent = {
  id: string
  name: string
  description: string
  status: "active" | "syncing" | "idle"
}

export type AgentListResponse = {
  count: number
  agents: Agent[]
}

export type Market = {
  symbol: string
  name: string
  price: number
  change_percent: number
  high: number
  low: number
  currency: string
  is_mock: boolean
}

export type MarketListResponse = {
  data_source: "mock"
  count: number
  markets: Market[]
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  })

  if (!response.ok) {
    throw new Error(`Nimbus API request failed with status ${response.status}`)
  }

  return response.json() as Promise<T>
}

export const getAgents = (signal?: AbortSignal) =>
  getJson<AgentListResponse>("/api/agents", signal)

export const getMarkets = (signal?: AbortSignal) =>
  getJson<MarketListResponse>("/api/markets", signal)
