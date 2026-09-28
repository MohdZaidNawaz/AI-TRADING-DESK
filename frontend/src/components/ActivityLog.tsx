const activities = [
  { time: "14:32:08", agent: "MACRO", tone: "violet", message: "Cross-asset regime map refreshed · 12 sources reconciled", meta: "COMPLETE" },
  { time: "14:31:42", agent: "RECON", tone: "blue", message: "Elevated momentum detected on XAUUSD · monitoring", meta: "WATCHING" },
  { time: "14:30:17", agent: "RISK", tone: "green", message: "Portfolio conditions remain within nominal range", meta: "NOMINAL" },
]

function ActivityLog() {
  return (
    <section className="activity-panel" aria-labelledby="activity-title">
      <div className="activity-heading">
        <div>
          <span className="eyebrow">REAL-TIME FEED</span>
          <h2 id="activity-title">SIGNAL LOG</h2>
        </div>
        <span className="activity-stream"><i /> STREAMING</span>
      </div>

      <div className="activity-list">
        {activities.map((activity) => (
          <article className="activity-item" key={`${activity.time}-${activity.agent}`}>
            <time>{activity.time}</time>
            <span className={`activity-agent tone-${activity.tone}`}><i />{activity.agent}</span>
            <p>{activity.message}</p>
            <span className="activity-meta">{activity.meta}</span>
          </article>
        ))}
      </div>
    </section>
  )
}

export default ActivityLog
