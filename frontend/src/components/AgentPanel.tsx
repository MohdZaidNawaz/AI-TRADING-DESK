import { useEffect, useState } from "react"
import type { CSSProperties } from "react"

import { FALLBACK_AGENTS } from "../data/fallbackData"
import { getAgents } from "../services/api"
import type { Agent, ApiConnectionState } from "../services/api"

const agentCodes: Record<string, string> = {
  macro: "MA",
  news: "NW",
  technical: "TA",
  quant: "QT",
  fundamental: "FA",
  risk: "RK",
  recon: "RC",
  journal: "JR",
}

const statusDisplay = {
  active: { label: "ACTIVE", className: "active" },
  syncing: { label: "SYNC", className: "sync" },
  idle: { label: "IDLE", className: "idle" },
} as const

const connectionLabels: Record<ApiConnectionState, string> = {
  loading: "CONNECTING",
  online: "API ONLINE",
  error: "LOCAL CACHE",
}

function AgentPanel() {
  const [agents, setAgents] = useState<Agent[]>(FALLBACK_AGENTS)
  const [connection, setConnection] = useState<ApiConnectionState>("loading")

  useEffect(() => {
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 4000)
    let mounted = true

    getAgents(controller.signal)
      .then((response) => {
        if (mounted && response.agents.length > 0) {
          setAgents(response.agents)
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
    <section className="panel agent-panel" aria-labelledby="agents-title">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">DESK ROSTER</span>
          <h2 id="agents-title">AGENT NETWORK</h2>
        </div>
        <span className="panel-count">{String(agents.length).padStart(2, "0")}</span>
      </div>

      <div className="agent-list">
        {agents.map((agent, index) => {
          const displayStatus = statusDisplay[agent.status]

          return (
            <article
              className="agent-card"
              key={agent.id}
              style={{ "--agent-index": index } as CSSProperties}
            >
              <span className="agent-code" aria-hidden="true">
                {agentCodes[agent.id] ?? agent.name.slice(0, 2).toUpperCase()}
              </span>

              <div className="agent-info">
                <strong>{agent.name.toUpperCase()}</strong>
                <span>{agent.description}</span>
              </div>

              <span className={`agent-status status-${displayStatus.className}`}>
                <i />
                {displayStatus.label}
              </span>
            </article>
          )
        })}
      </div>

      <div className="network-status">
        <span aria-live="polite">{connectionLabels[connection]}</span>
        <div className="load-track"><i /></div>
        <strong>42%</strong>
      </div>
    </section>
  )
}

export default AgentPanel
