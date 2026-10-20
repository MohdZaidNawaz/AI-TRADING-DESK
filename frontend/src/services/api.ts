const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/$/, "")

export type ApiConnectionState = "loading" | "online" | "error"

export type HealthResponse = {
  status: "ok"
  service: string
  version: string
}

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

export type ProductionReadinessCheck = {
  prerequisite: string
  status: string
  reason_codes: string[]
  source_reference: string | null
  checked_at: string
  authorizes_execution: false
}

export type ProductionReadinessReport = {
  schema_version: "production-authorization-readiness/v1"
  status: "blocked"
  evaluated_at: string
  checks: ProductionReadinessCheck[]
  reason_codes: string[]
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
  fingerprint: string
}

export type AgentAnalysis = {
  agent_id: string
  agent_name: string
  status: "completed" | "skipped" | "error"
  summary: string
  findings: string[]
  confidence: number
  limitations: string[]
}

export type AnalyzeResponse = {
  analysis_id: string
  mode: "deterministic_mock"
  symbol: string | null
  query: string
  selected_agents: string[]
  routing: {
    selected_agents: string[]
    reason: string
  }
  agent_results: AgentAnalysis[]
  synthesis: {
    status: "completed"
    agents_consulted: string[]
    summary: string
    final_response: string
    caveat: string
  }
}

async function requestJson<T>(
  path: string,
  init: RequestInit,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...init.headers,
    },
    signal,
  })

  if (!response.ok) {
    throw new Error(`Nimbus API request failed with status ${response.status}`)
  }

  const contentType = response.headers.get("content-type") ?? ""
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new Error("Nimbus API returned an unexpected response format")
  }

  return response.json() as Promise<T>
}

const getJson = <T>(path: string, signal?: AbortSignal) =>
  requestJson<T>(path, { method: "GET" }, signal)

export const getHealth = (signal?: AbortSignal) =>
  getJson<HealthResponse>("/api/health", signal)

export const getProductionReadiness = (signal?: AbortSignal) =>
  getJson<ProductionReadinessReport>("/api/readiness", signal)

export const getAgents = (signal?: AbortSignal) =>
  getJson<AgentListResponse>("/api/agents", signal)

export const getMarkets = (signal?: AbortSignal) =>
  getJson<MarketListResponse>("/api/markets", signal)

export const analyzeDeskQuery = (query: string, signal?: AbortSignal) =>
  requestJson<AnalyzeResponse>(
    "/api/analyze",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    },
    signal,
  )
