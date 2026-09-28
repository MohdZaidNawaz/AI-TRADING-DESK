import { useEffect, useState } from "react"

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

function Header() {
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <header className="top-header">
      <div className="brand">
        <div className="brand-copy">
          <h1>FRIDAY</h1>
          <span>AI TRADING INTELLIGENCE</span>
          <small><i /> SYSTEM ONLINE <b>·</b> CORE READY</small>
        </div>
      </div>

      <div className="header-center" aria-hidden="true">
        <span><b>8</b> AGENTS</span><em>//</em>
        <span><b>3</b> FEEDS</span><em>//</em>
        <span><b>248</b> SCANS</span><em>//</em>
        <span><b>61</b> SIGNALS</span><em>//</em>
        <span className="uptime"><b>00:13</b> UPTIME</span>
      </div>

      <div className="desk-clock">
        <strong>{formatClock(now)}</strong>
        <span>{formatDate(now)} <b>//</b> LOCAL</span>
        <small><i /> DESK SERVICES · CONNECTED</small>
      </div>
    </header>
  )
}

export default Header
