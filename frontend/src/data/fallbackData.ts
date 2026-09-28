import type { Agent, Market } from "../services/api"

export const FALLBACK_AGENTS: Agent[] = [
  { id: "macro", name: "Macro", description: "Global regime analysis", status: "active" },
  { id: "news", name: "News", description: "News and event intelligence", status: "syncing" },
  { id: "technical", name: "Technical", description: "Price structure and momentum", status: "active" },
  { id: "quant", name: "Quant", description: "Statistical models and probability", status: "active" },
  { id: "fundamental", name: "Fundamental", description: "Value and catalyst analysis", status: "idle" },
  { id: "risk", name: "Risk", description: "Risk and exposure oversight", status: "active" },
  { id: "recon", name: "Recon", description: "Market surveillance", status: "syncing" },
  { id: "journal", name: "Journal", description: "Desk intelligence memory", status: "idle" },
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
