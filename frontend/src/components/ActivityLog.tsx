import type {
  AnalyzeResponse,
  ApiConnectionState,
  ProductionReadinessReport,
} from "../services/api"

type ActivityLogProps = {
  connection: ApiConnectionState
  readiness: ProductionReadinessReport | null
  analysis: AnalyzeResponse | null
}

const apiStatus: Record<ApiConnectionState, string> = {
  loading: "Backend status is being checked",
  online: "Backend liveness and readiness endpoints responded",
  error: "Backend is unavailable; static labels remain visible",
}

function ActivityLog({ connection, readiness, analysis }: ActivityLogProps) {
  const activities = [
    {
      agent: "SYSTEM",
      tone: connection === "online" ? "green" : "blue",
      message: apiStatus[connection],
      meta: connection.toUpperCase(),
    },
    {
      agent: "SENTINEL",
      tone: "violet",
      message: readiness
        ? `Production blocked by ${readiness.reason_codes.length} explicit readiness conditions`
        : "No production-readiness report is available",
      meta: readiness ? "BLOCKED" : "UNKNOWN",
    },
    {
      agent: "FRIDAY",
      tone: "blue",
      message: analysis
        ? analysis.synthesis.final_response
        : "No analysis submitted in this browser session",
      meta: analysis ? "DETERMINISTIC" : "IDLE",
    },
  ]

  return (
    <section className="activity-panel" aria-labelledby="activity-title">
      <div className="activity-heading">
        <div>
          <span className="eyebrow">SESSION FACTS</span>
          <h2 id="activity-title">DESK STATUS</h2>
        </div>
        <span className="activity-stream"><i /> NOT STREAMING</span>
      </div>

      <div className="activity-list" aria-live="polite">
        {activities.map((activity) => (
          <article className="activity-item" key={activity.agent}>
            <time>NOW</time>
            <span className={`activity-agent tone-${activity.tone}`}><i />{activity.agent}</span>
            <p title={activity.message}>{activity.message}</p>
            <span className="activity-meta">{activity.meta}</span>
          </article>
        ))}
      </div>
    </section>
  )
}

export default ActivityLog
