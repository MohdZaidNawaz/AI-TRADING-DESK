const coreAgents = [
  { name: "MACRO", role: "REGIME", position: "node-top-left", tone: "blue" },
  { name: "NEWS", role: "EVENTS", position: "node-top", tone: "gold" },
  { name: "RECON", role: "SCOUT", position: "node-top-right", tone: "orange" },
  { name: "RISK", role: "SENTINEL", position: "node-right", tone: "red" },
  { name: "JOURNAL", role: "MEMORY", position: "node-bottom-right", tone: "green" },
  { name: "QUANT", role: "MODELS", position: "node-bottom-left", tone: "violet" },
  { name: "TECHNICAL", role: "STRUCTURE", position: "node-left", tone: "cyan" },
  { name: "FUNDAMENTAL", role: "VALUE", position: "node-mid-left", tone: "teal" },
]

function AICore() {
  return (
    <section className="ai-core" aria-labelledby="core-title">
      <div className="core-topline">
        <span>MASTER INTELLIGENCE / SYNTHESIS</span>
        <span className="core-sequence">FOCUS MODE</span>
      </div>

      <div className="core-stage">
        <div className="core-crosshair crosshair-x" aria-hidden="true" />
        <div className="core-crosshair crosshair-y" aria-hidden="true" />
        <div className="core-scanline" aria-hidden="true" />

        <svg className="node-links" viewBox="0 0 1000 700" preserveAspectRatio="none" aria-hidden="true">
          <path d="M500 338L245 160M500 338L500 84M500 338L760 160M500 338L868 340M500 338L760 582M500 338L260 590M500 338L132 340M500 338L180 252" />
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
          <span className="core-role">MASTER · STRATEGY SYNTHESIS · RISK ROUTER</span>
          <div className="core-metrics">
            <span><strong>8</strong><small>ONLINE</small></span>
            <span><strong>248</strong><small>SCANS</small></span>
            <span><strong className="metric-cyan">61</strong><small>SIGNALS</small></span>
            <span><strong>94%</strong><small>CONFIDENCE</small></span>
          </div>
        </div>

        <div className="telemetry telemetry-left" aria-hidden="true">
          <span>DESK STATE</span>
          <strong>ACTIVE</strong>
        </div>
        <div className="telemetry telemetry-right" aria-hidden="true">
          <span>RESPONSE</span>
          <strong>12.4 <small>MS</small></strong>
        </div>
      </div>

      <div className="core-footer">
        <span><i /> 8 AGENTS LINKED</span>
        <span className="core-state"><i /> CORE READY</span>
        <span><i /> DATA STREAM LIVE</span>
      </div>
    </section>
  )
}

export default AICore
