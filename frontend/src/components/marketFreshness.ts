import type { Market } from "../services/api"

type LiveFreshness = "live" | "delayed" | "stale"

/** Return the least-fresh state represented by a live provider snapshot. */
export function getAggregateLiveFreshness(
  markets: readonly Pick<Market, "freshness">[],
): LiveFreshness {
  let hasDelayedQuote = false

  for (const market of markets) {
    if (market.freshness === "stale") return "stale"
    if (market.freshness === "delayed") hasDelayedQuote = true
  }

  return hasDelayedQuote ? "delayed" : "live"
}
