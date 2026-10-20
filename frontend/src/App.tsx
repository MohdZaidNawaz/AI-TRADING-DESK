import { useEffect, useState } from "react"

import ActivityLog from "./components/ActivityLog"
import AgentPanel from "./components/AgentPanel"
import AICore from "./components/AICore"
import CommandBar from "./components/CommandBar"
import Header from "./components/Header"
import MarketPanel from "./components/MarketPanel"
import { getHealth, getProductionReadiness } from "./services/api"
import type {
  AnalyzeResponse,
  ApiConnectionState,
  ProductionReadinessReport,
} from "./services/api"
import "./App.css"

function App() {
  const [connection, setConnection] = useState<ApiConnectionState>("loading")
  const [readiness, setReadiness] = useState<ProductionReadinessReport | null>(null)
  const [analysis, setAnalysis] = useState<AnalyzeResponse | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 5000)
    let mounted = true

    Promise.all([
      getHealth(controller.signal),
      getProductionReadiness(controller.signal),
    ])
      .then(([, readinessReport]) => {
        if (!mounted) return
        setReadiness(readinessReport)
        setConnection("online")
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
    <div className="app-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />

      <Header connection={connection} readiness={readiness} />

      <main className="workspace">
        <div className="dashboard-grid">
          <AgentPanel />
          <AICore connection={connection} readiness={readiness} analysis={analysis} />
          <MarketPanel />
        </div>

        <ActivityLog connection={connection} readiness={readiness} analysis={analysis} />
      </main>

      <CommandBar onAnalysis={setAnalysis} />
    </div>
  )
}

export default App
