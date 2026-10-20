import { useEffect, useState } from "react"

import type { ApiConnectionState, ProductionReadinessReport } from "../services/api"

type HeaderProps = {
  connection: ApiConnectionState
  readiness: ProductionReadinessReport | null
}

const connectionLabel: Record<ApiConnectionState, string> = {
  loading: "API CHECKING",
  online: "API ONLINE",
  error: "API UNAVAILABLE",
}

const formatClock = (date: Date) =>
  new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date)

const formatDate = (date: Date) =>
  new Intl.DateTimeFormat("en-GB", {
    weekday: "long",
    day: "2-digit",
    month: "long",
  }).format(date).toUpperCase()

function Header({ connection, readiness }: HeaderProps) {
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const executionLabel = readiness ? "EXECUTION BLOCKED" : "READINESS UNKNOWN"

  return (
    <header className="top-header">
      <div className="brand">
        <div className="brand-copy">
          <h1>FRIDAY</h1>
          <span>AI TRADING INTELLIGENCE</span>
          <small className={`header-status-${connection}`}><i /> {connectionLabel[connection]} <b>·</b> {executionLabel}</small>
        </div>
      </div>

      <div className="header-center" aria-label="Desk capability summary">
        <span><b>9</b> SPECIALISTS</span><em>//</em>
        <span><b>MOCK</b> MARKETS</span><em>//</em>
        <span><b>DETERMINISTIC</b> ANALYSIS</span><em>//</em>
        <span><b>NO</b> EXECUTION</span>
      </div>

      <div className="desk-clock">
        <strong>{formatClock(now)}</strong>
        <span>{formatDate(now)} <b>//</b> LOCAL</span>
        <small><i /> RESEARCH DESK · NON-EXECUTING</small>
      </div>
    </header>
  )
}

export default Header
