export const NAVIGATION = [
  { id: "home", label: "Home" },
  { id: "ai-intelligence", label: "AI Intelligence" },
  { id: "market-analysis", label: "Market Analysis" },
  { id: "markets", label: "Markets & Watchlist" },
  { id: "trading-journal", label: "Trading Journal" },
  { id: "conversations", label: "Recent Conversations" },
  { id: "saved-analyses", label: "Saved Analyses" },
  { id: "economic-calendar", label: "Economic Calendar" },
  { id: "settings", label: "Settings" },
] as const

export type Screen = typeof NAVIGATION[number]["id"]

export function screenFromHash(hash: string): Screen {
  return NAVIGATION.find((item) => `#${item.id}` === hash)?.id ?? "home"
}