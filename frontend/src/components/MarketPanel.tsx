import { useEffect, useState } from "react"

import { FALLBACK_MARKETS } from "../data/fallbackData"
import { getMarkets } from "../services/api"
import type { ApiConnectionState, Market } from "../services/api"

const risingChart = "2,31 11,27 20,29 29,20 38,23 47,14 56,17 65,8 74,13 83,7 92,9 101,3"
const fallingChart = "2,6 11,10 20,8 29,17 38,14 47,23 56,18 65,26 74,23 83,31 92,27 101,34"

const connectionLabels: Record<ApiConnectionState, string> = {
  loading: "CONNECTING",
  online: "API ONLINE",
  error: "CACHED",
}

const decimalsBySymbol: Record<string, number> = {
  EURUSD: 4,
  XAUUSD: 2,
  BTCUSD: 2,
}

const formatMarketValue = (market: Market, value: number) =>
  new Intl.NumberFormat("en-US", {
    minimumFractionDigits: decimalsBySymbol[market.symbol] ?? 2,
    maximumFractionDigits: decimalsBySymbol[market.symbol] ?? 2,
  }).format(value)

const formatChange = (change: number) =>
  `${change >= 0 ? "+" : "−"}${Math.abs(change).toFixed(2)}%`

function Sparkline({ market }: { market: Market }) {
  const direction = market.change_percent >= 0 ? "up" : "down"
  const chart = direction === "up" ? risingChart : fallingChart

  return (
    <svg className="sparkline" viewBox="0 0 104 38" role="img" aria-label={`${market.symbol} ${direction === "up" ? "rising" : "falling"}`}>
      <defs>
        <linearGradient id={`fill-${market.symbol}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="currentColor" stopOpacity=".24" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={`${chart} 101,38 2,38`} fill={`url(#fill-${market.symbol})`} />
      <polyline points={chart} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function MarketPanel() {
  const [markets, setMarkets] = useState<Market[]>(FALLBACK_MARKETS)
  const [connection, setConnection] = useState<ApiConnectionState>("loading")

  useEffect(() => {
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 4000)
    let mounted = true

    getMarkets(controller.signal)
      .then((response) => {
        if (mounted && response.markets.length > 0) {
          setMarkets(response.markets)
          setConnection("online")
        }
      })
      .catch(() => {
        if (mounted) setConnection("error")
      })
      .finally(() => window.clearTimeout(timeout))

    return () => {
      mounted = false
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [])

  return (
    <section className="panel market-panel" aria-labelledby="market-title">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">MARKET PULSE</span>
          <h2 id="market-title">MARKET DATA</h2>
        </div>
        <span className="live-badge" aria-live="polite"><i /> {connectionLabels[connection]}</span>
      </div>

      <div className="market-list">
        {markets.map((market) => {
          const direction = market.change_percent >= 0 ? "up" : "down"

          return (
            <article className="market-card" key={market.symbol}>
              <div className="market-title-row">
                <div>
                  <strong>{market.symbol}</strong>
                  <span>{market.name}</span>
                </div>
                <span className={`market-change ${direction}`}>{formatChange(market.change_percent)}</span>
              </div>
              <div className="market-price-row">
                <span className="market-price">{formatMarketValue(market, market.price)}</span>
                <Sparkline market={market} />
              </div>
              <div className="market-meta">
                <span>H <b>{formatMarketValue(market, market.high)}</b></span>
                <span>L <b>{formatMarketValue(market, market.low)}</b></span>
                <span>24H</span>
              </div>
            </article>
          )
        })}
      </div>

      <div className="market-footer">
        <span>BACKEND FEED</span>
        <strong>MOCK / {connectionLabels[connection]}</strong>
      </div>
    </section>
  )
}

export default MarketPanel
