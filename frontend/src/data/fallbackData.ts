import type { Agent, Market } from "../services/api"

export const FALLBACK_AGENTS: Agent[] = [
  { id: "atlas_macro", name: "Atlas Macro", description: "Global regime, rates, and cross-asset context", status: "active" },
  { id: "capitol_smart_money", name: "Capitol Smart Money", description: "Institutional positioning and capital-flow research", status: "idle" },
  { id: "scout_recon", name: "Scout Recon", description: "Market surveillance and catalyst reconnaissance", status: "syncing" },
  { id: "athena_analyst", name: "Athena Analyst", description: "Fundamental context and catalyst analysis", status: "idle" },
  { id: "chartist_technician", name: "Chartist Technician", description: "Price structure, trend, and momentum", status: "active" },
  { id: "oracle_quant", name: "Oracle Quant", description: "Statistical models and probability research", status: "active" },
  { id: "sentinel_risk_officer", name: "Sentinel Risk Officer", description: "Research-risk and exposure oversight", status: "active" },
  { id: "pilot_execution", name: "Pilot Execution", description: "Disabled future execution coordination", status: "idle" },
  { id: "ledger_the_book", name: "Ledger The Book", description: "Research memory and decision review", status: "idle" },
]

export const FALLBACK_MARKETS: Market[] = [
  {
    symbol: "XAUUSD",
    name: "Gold / US Dollar",
    price: 2648.32,
    change_percent: 0.42,
    high: 2661,
    low: 2629,
    currency: "USD",
    is_mock: true,
  },
  {
    symbol: "EURUSD",
    name: "Euro / US Dollar",
    price: 1.0842,
    change_percent: -0.18,
    high: 1.0871,
    low: 1.0818,
    currency: "USD",
    is_mock: true,
  },
  {
    symbol: "BTCUSD",
    name: "Bitcoin / US Dollar",
    price: 67284.1,
    change_percent: 1.24,
    high: 67910,
    low: 65832,
    currency: "USD",
    is_mock: true,
  },
]
