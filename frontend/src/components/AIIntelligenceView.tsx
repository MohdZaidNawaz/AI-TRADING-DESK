import { useEffect, useMemo, useState } from "react"

import { FALLBACK_AGENTS } from "../data/fallbackData"
import { getAgents } from "../services/api"
import type { Agent, AgentAnalysis, AgentExecutionState, AnalyzeResponse } from "../services/api"
import type { AssistantVisualState } from "./assistantState"

type IntelligenceViewProps = {
  analysis: AnalyzeResponse | null
  assistantState: AssistantVisualState
  currentCommand: string
  lastQuery: string | null
}

type RosterState = "loading" | "available" | "unavailable"
type ExecutionState = AgentExecutionState | "idle"

const agentCodes: Record<string, string> = {
  atlas_macro: "AT",
  capitol_smart_money: "CP",
  scout_recon: "SC",
  athena_analyst: "AH",
  chartist_technician: "CH",
  oracle_quant: "OR",
  sentinel_risk_officer: "SN",
  pilot_execution: "PL",
  ledger_the_book: "LD",
}

function executionState(
  result: AgentAnalysis | undefined,
  rosterState: RosterState,
): ExecutionState {
  if (result?.execution_state) return result.execution_state
  if (result?.status === "completed") return "completed"
  if (result?.status === "error") return "failed"
  if (result?.status === "skipped") return "unavailable"
  if (rosterState !== "available") return "unavailable"
  return "idle"
}

const statusLabels: Record<ExecutionState, string> = {
  idle: "Idle",
  pending: "Pending",
  running: "Running",
  completed: "Completed",
  partial: "Partial",
  failed: "Failed",
  unavailable: "Unavailable",
}

function AIIntelligenceView({
  analysis,
  assistantState,
  currentCommand,
  lastQuery,
}: IntelligenceViewProps) {
  const [agents, setAgents] = useState<Agent[]>(FALLBACK_AGENTS)
  const [rosterState, setRosterState] = useState<RosterState>("loading")
  const [selectedAgentId, setSelectedAgentId] = useState(FALLBACK_AGENTS[0].id)

  useEffect(() => {
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 5000)
    let mounted = true

    getAgents(controller.signal)
      .then((response) => {
        if (!mounted) return
        if (response.agents.length > 0) {
          setAgents(response.agents)
          setRosterState("available")
          setSelectedAgentId((current) => (
            response.agents.some((agent) => agent.id === current)
              ? current
              : response.agents[0].id
          ))
        } else {
          setRosterState("unavailable")
        }
      })
      .catch(() => {
        if (mounted) setRosterState("unavailable")
      })
      .finally(() => window.clearTimeout(timeout))

    return () => {
      mounted = false
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [])

  const results = useMemo(
    () => new Map(analysis?.agent_results.map((result) => [result.agent_id, result]) ?? []),
    [analysis],
  )
  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId) ?? agents[0]
  const selectedResult = selectedAgent ? results.get(selectedAgent.id) : undefined
  const selectedState = selectedAgent
    ? executionState(selectedResult, rosterState)
    : "unavailable"
  const requestActive = assistantState === "thinking" || assistantState === "analysing"
  const requestText = requestActive ? currentCommand.trim() : lastQuery
  const executions = analysis?.coordination?.executions ?? []
  const completedCount = executions.filter((item) => item.state === "completed").length
  const partialCount = executions.filter((item) => item.state === "partial").length
  const failedCount = executions.filter((item) => item.state === "failed").length
  const unavailableCount = executions.filter((item) => item.state === "unavailable").length

  return (
    <div className="phase2-workspace intelligence-workspace">
      <header className="phase2-page-heading">
        <div>
          <span className="phase2-kicker">MULTI-AGENT INTELLIGENCE</span>
          <h1>AI Intelligence Command Centre</h1>
          <p>Inspect FRIDAY&apos;s registered specialists and the latest deterministic research route.</p>
        </div>
        <div className={`phase2-mode mode-${requestActive ? "active" : "idle"}`}>
          <i />
          <span>{requestActive ? "REQUEST IN PROGRESS" : "NON-EXECUTING RESEARCH"}</span>
        </div>
      </header>

      <section className="intelligence-command-grid" aria-label="Agent intelligence architecture">
        <div className="agent-constellation">
          <div className="constellation-grid" aria-hidden="true" />
          <svg className="constellation-links" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
            {agents.map((agent, index) => {
              const angle = (-90 + index * 40) * (Math.PI / 180)
              return (
                <line
                  key={agent.id}
                  x1="50"
                  y1="50"
                  x2={50 + Math.cos(angle) * 39}
                  y2={50 + Math.sin(angle) * 39}
                />
              )
            })}
          </svg>

          <div className={`strategist-hub ${requestActive ? "is-running" : analysis ? "is-completed" : "is-idle"}`}>
            <span className="strategist-ring" aria-hidden="true" />
            <div>
              <small>ORCHESTRATION / SYNTHESIS</small>
              <strong>CHIEF MARKET<br />STRATEGIST</strong>
              <span>{requestActive ? "Request in progress" : analysis ? "Synthesis available" : "Awaiting research"}</span>
            </div>
          </div>

          <div className="constellation-agents">
            {agents.map((agent, index) => {
              const result = results.get(agent.id)
              const state = executionState(result, rosterState)
              return (
                <button
                  type="button"
                  className={`constellation-agent agent-position-${index + 1} state-${state}${selectedAgentId === agent.id ? " is-selected" : ""}`}
                  key={agent.id}
                  onClick={() => setSelectedAgentId(agent.id)}
                  aria-pressed={selectedAgentId === agent.id}
                  aria-label={`${agent.name}, ${statusLabels[state]}`}
                >
                  <span>{agentCodes[agent.id] ?? agent.name.slice(0, 2).toUpperCase()}</span>
                  <strong>{agent.name}</strong>
                  <small><i />{statusLabels[state]}</small>
                </button>
              )
            })}
          </div>
        </div>

        <aside className="agent-inspector" aria-labelledby="agent-inspector-title">
          <div className="phase2-panel-heading">
            <div>
              <span>SELECTED SPECIALIST</span>
              <h2 id="agent-inspector-title">{selectedAgent?.name ?? "Agent unavailable"}</h2>
            </div>
            <b className={`phase2-status status-${selectedState}`}>{statusLabels[selectedState]}</b>
          </div>
          <p className="agent-responsibility">{selectedAgent?.description ?? "No registered specialist details are available."}</p>

          <dl className="agent-facts">
            <div><dt>Registry</dt><dd>{rosterState === "available" ? "API registered" : rosterState === "loading" ? "Loading" : "Unavailable"}</dd></div>
            <div><dt>Latest route</dt><dd>{analysis?.selected_agents.includes(selectedAgent?.id ?? "") ? "Selected" : "Not selected"}</dd></div>
            <div><dt>Result state</dt><dd>{selectedResult?.execution_state ?? selectedResult?.status ?? "No session result"}</dd></div>
            <div><dt>Timestamp</dt><dd>{selectedResult?.completed_at ? new Date(selectedResult.completed_at).toLocaleString() : "Not supplied"}</dd></div>
          </dl>

          <section className="agent-result-block" aria-label="Latest specialist result">
            <h3>LATEST AVAILABLE ACTIVITY</h3>
            {selectedResult ? (
              <>
                <p>{selectedResult.summary}</p>
                {selectedResult.findings.length > 0 && (
                  <ul>{selectedResult.findings.map((finding) => <li key={finding}>{finding}</li>)}</ul>
                )}
                <div className="agent-result-meta">
                  <span>Backend confidence <strong>{selectedResult.confidence.toFixed(2)}</strong></span>
                  <span>Mode <strong>{analysis?.mode === "deterministic_mock" ? "Mock" : "Unavailable"}</strong></span>
                </div>
                {(selectedResult.evidence_references?.length ?? 0) > 0 && (
                  <div className="agent-limitations">
                    <b>EVIDENCE REFERENCES</b>
                    {selectedResult.evidence_references?.map((reference) => (
                      <p key={`${reference.source_id}:${reference.event_id}`}>
                        {reference.source_id} · {reference.domain} · {reference.observed_at} · {reference.freshness}
                      </p>
                    ))}
                  </div>
                )}
                {(selectedResult.missing_inputs?.length ?? 0) > 0 && (
                  <div className="agent-limitations">
                    <b>MISSING INPUTS</b>
                    <p>{selectedResult.missing_inputs?.join(" · ")}</p>
                  </div>
                )}
                {selectedResult.limitations.length > 0 && (
                  <div className="agent-limitations">
                    <b>LIMITATIONS</b>
                    {selectedResult.limitations.map((limitation) => <p key={limitation}>{limitation}</p>)}
                  </div>
                )}
              </>
            ) : (
              <div className="phase2-empty-state">
                <span>NO RESULT</span>
                <p>This specialist has no analysis result in the current browser session.</p>
              </div>
            )}
          </section>
        </aside>
      </section>

      <section className="execution-overview" aria-labelledby="execution-overview-title">
        <div className="phase2-panel-heading">
          <div><span>RESEARCH TELEMETRY</span><h2 id="execution-overview-title">Intelligence Execution Overview</h2></div>
          <b>{analysis ? analysis.analysis_id : "NO SESSION ANALYSIS"}</b>
        </div>
        <div className="execution-overview-grid">
          <article>
            <small>CURRENT REQUEST</small>
            <p>{requestText || "No research request has been submitted in this browser session."}</p>
            <span>{requestActive ? "Routing outcome pending" : analysis?.routing.reason ?? "No routing record"}</span>
          </article>
          <article className="execution-counts">
            <span><strong>{analysis?.selected_agents.length ?? 0}</strong><small>INVOKED</small></span>
            <span><strong>{completedCount}</strong><small>COMPLETED</small></span>
            <span><strong>{partialCount}</strong><small>PARTIAL</small></span>
            <span><strong>{failedCount}</strong><small>FAILED</small></span>
            <span><strong>{unavailableCount}</strong><small>UNAVAILABLE</small></span>
          </article>
          <article>
            <small>STRATEGIST SYNTHESIS</small>
            <p>{analysis?.synthesis.summary ?? "A synthesis will appear after the analysis API returns a completed response."}</p>
            <span>{analysis?.synthesis.caveat ?? "No explicit conflict field or source timestamp is supplied by the current contract."}</span>
            {(analysis?.synthesis.disagreement_agent_ids?.length ?? 0) > 0 && (
              <span>Disagreement: {analysis?.synthesis.disagreement_agent_ids?.join(", ")}</span>
            )}
            {(analysis?.synthesis.missing_agent_ids?.length ?? 0) > 0 && (
              <span>Missing: {analysis?.synthesis.missing_agent_ids?.join(", ")}</span>
            )}
          </article>
        </div>
      </section>
    </div>
  )
}

export default AIIntelligenceView
