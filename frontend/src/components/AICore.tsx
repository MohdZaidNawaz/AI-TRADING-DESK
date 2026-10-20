import type {
  AnalyzeResponse,
  ApiConnectionState,
  ProductionReadinessReport,
} from "../services/api"

type AICoreProps = {
  connection: ApiConnectionState
  readiness: ProductionReadinessReport | null
  analysis: AnalyzeResponse | null
}

const coreAgents = [
  { name: "ATLAS", role: "MACRO", position: "node-top-left", tone: "blue" },
  { name: "CAPITOL", role: "POSITIONING", position: "node-top", tone: "gold" },
  { name: "SCOUT", role: "RECON", position: "node-top-right", tone: "orange" },
  { name: "SENTINEL", role: "RISK", position: "node-right", tone: "red" },
  { name: "PILOT", role: "DISABLED", position: "node-bottom-right", tone: "green" },
  { name: "LEDGER", role: "PROVENANCE", position: "node-bottom", tone: "teal" },
  { name: "ORACLE", role: "QUANT", position: "node-bottom-left", tone: "violet" },
  { name: "CHARTIST", role: "TECHNICAL", position: "node-left", tone: "cyan" },
  { name: "ATHENA", role: "ANALYSIS", position: "node-mid-left", tone: "teal" },
]

const apiLabel: Record<ApiConnectionState, string> = {
  loading: "CHECKING",
  online: "ONLINE",
  error: "UNAVAILABLE",
}

function AICore({ connection, readiness, analysis }: AICoreProps) {
  const consulted = analysis?.synthesis.agents_consulted.length ?? 0

  return (
    <section className="ai-core" aria-labelledby="core-title">
      <div className="core-topline">
        <span>MASTER INTELLIGENCE / SYNTHESIS</span>
        <span className="core-sequence">RESEARCH MODE</span>
      </div>

      <div className="core-stage">
        <div className="core-crosshair crosshair-x" aria-hidden="true" />
        <div className="core-crosshair crosshair-y" aria-hidden="true" />
        <div className="core-scanline" aria-hidden="true" />

        <svg className="node-links" viewBox="0 0 1000 700" preserveAspectRatio="none" aria-hidden="true">
          <path d="M500 338L245 160M500 338L500 84M500 338L760 160M500 338L868 340M500 338L760 582M500 338L500 650M500 338L260 590M500 338L132 340M500 338L180 252" />
        </svg>

        <div className="core-agent-nodes" aria-hidden="true">
          {coreAgents.map((agent) => (
            <div className={`core-agent-node ${agent.position} tone-${agent.tone}`} key={agent.name}>
              <span><i /></span>
              <strong>{agent.name}</strong>
              <small>{agent.role}</small>
            </div>
          ))}
        </div>

        <div className="orbit orbit-outer" aria-hidden="true">
          <span className="orbit-node node-one" />
          <span className="orbit-node node-two" />
        </div>
        <div className="orbit orbit-middle" aria-hidden="true">
          <span className="orbit-tick tick-one" />
          <span className="orbit-tick tick-two" />
          <span className="orbit-tick tick-three" />
        </div>
        <div className="orbit orbit-inner" aria-hidden="true" />

        <div className="core-orb" aria-hidden="true">
          <div className="orb-grid" />
          <div className="orb-flare" />
          <span className="orb-letter" />
        </div>

        <div className="core-caption">
          <h2 id="core-title">FRIDAY</h2>
          <span className="core-role">MASTER · SYNTHESIS · NON-EXECUTING</span>
          <div className="core-metrics">
            <span><strong>9</strong><small>SPECIALISTS</small></span>
            <span><strong>{consulted}</strong><small>CONSULTED</small></span>
            <span><strong className="metric-cyan">{analysis ? "MOCK" : "—"}</strong><small>LAST MODE</small></span>
            <span><strong>OFF</strong><small>EXECUTION</small></span>
          </div>
        </div>

        <div className="telemetry telemetry-left" aria-hidden="true">
          <span>DESK MODE</span>
          <strong>RESEARCH</strong>
        </div>
        <div className="telemetry telemetry-right" aria-hidden="true">
          <span>LATENCY</span>
          <strong>NOT <small>MEASURED</small></strong>
        </div>
      </div>

      <div className="core-footer">
        <span><i /> 9 SPECIALISTS DEFINED</span>
        <span className="core-state"><i /> API {apiLabel[connection]}</span>
        <span><i /> {readiness ? "EXECUTION BLOCKED" : "READINESS UNKNOWN"}</span>
      </div>
    </section>
  )
}

export default AICore
