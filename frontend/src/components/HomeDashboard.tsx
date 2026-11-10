import type {
  AnalyzeResponse,
  ApiConnectionState,
  ProductionReadinessReport,
} from "../services/api"
import ActivityLog from "./ActivityLog"
import AgentPanel from "./AgentPanel"
import AICore from "./AICore"
import CommandBar from "./CommandBar"
import ConversationPanel from "./ConversationPanel"
import type { AssistantVisualState } from "./assistantState"

export type ConversationSummary = {
  id: string
  query: string
  response: string
  agentsConsulted: number
}

type HomeDashboardProps = {
  analysis: AnalyzeResponse | null
  assistantState: AssistantVisualState
  command: string
  connection: ApiConnectionState
  conversations: ConversationSummary[]
  lastQuery: string | null
  readiness: ProductionReadinessReport | null
  voiceEnabled: boolean
  voiceError: string | null
  voiceSupported: boolean
  onAnalysis: (analysis: AnalyzeResponse, query: string) => boolean
  onCommandChange: (command: string) => void
  onStateChange: (state: AssistantVisualState) => void
  onStopSpeaking: () => void
  onToggleVoice: () => void
}

const quickActions = [
  {
    label: "Analyse Gold",
    detail: "Prepare a deterministic multi-agent research request.",
    command: "Analyse gold using the available deterministic research context.",
    code: "AU",
  },
  {
    label: "Today's Events",
    detail: "Ask for locally available event context without live polling.",
    command: "Summarise today's scheduled market events using available local context.",
    code: "EV",
  },
] as const

function SpecialistActivity({ analysis }: { analysis: AnalyzeResponse | null }) {
  const consulted = analysis?.synthesis.agents_consulted ?? []

  return (
    <section className="home-card specialist-activity" aria-labelledby="specialist-activity-title">
      <div className="home-card-heading">
        <div>
          <span className="eyebrow">SPECIALIST ACTIVITY</span>
          <h2 id="specialist-activity-title">LATEST RESEARCH ROUTE</h2>
        </div>
        <span>{consulted.length ? `${consulted.length} CONSULTED` : "SESSION IDLE"}</span>
      </div>
      {consulted.length ? (
        <ul>
          {consulted.map((agent) => <li key={agent}><i />{agent.replaceAll("_", " ")}</li>)}
        </ul>
      ) : (
        <p>No specialist analysis has run in this browser session.</p>
      )}
      <small>Agent presence is shown only after a completed deterministic analysis.</small>
    </section>
  )
}

function RecentConversations({ conversations }: { conversations: ConversationSummary[] }) {
  return (
    <section className="home-card recent-conversations" aria-labelledby="recent-conversations-title">
      <div className="home-card-heading">
        <div>
          <span className="eyebrow">SESSION MEMORY</span>
          <h2 id="recent-conversations-title">RECENT CONVERSATIONS</h2>
        </div>
        <span>{conversations.length ? `${conversations.length} SAVED` : "EMPTY"}</span>
      </div>
      {conversations.length ? (
        <div className="recent-conversation-list">
          {conversations.map((conversation) => (
            <article key={conversation.id}>
              <div><strong>{conversation.query}</strong><span>{conversation.agentsConsulted} specialist routes</span></div>
              <p>{conversation.response}</p>
            </article>
          ))}
        </div>
      ) : (
        <p>Completed research conversations from this session will appear here.</p>
      )}
    </section>
  )
}

function IntelligenceOverview({ analysis }: { analysis: AnalyzeResponse | null }) {
  return (
    <section className="home-card intelligence-overview" aria-labelledby="intelligence-overview-title">
      <div className="home-card-heading">
        <div>
          <span className="eyebrow">INTELLIGENCE OVERVIEW</span>
          <h2 id="intelligence-overview-title">CURRENT RESEARCH CONTEXT</h2>
        </div>
        <span>{analysis ? "AVAILABLE" : "AWAITING COMMAND"}</span>
      </div>
      <p>
        {analysis
          ? analysis.synthesis.summary
          : "Submit a command to create a deterministic, non-executing intelligence summary."}
      </p>
      <div className="intelligence-facts" aria-label="Current intelligence facts">
        <span><strong>{analysis?.symbol ?? "—"}</strong><small>INSTRUMENT</small></span>
        <span><strong>{analysis?.synthesis.agents_consulted.length ?? 0}</strong><small>SPECIALISTS</small></span>
        <span><strong>{analysis ? "MOCK" : "—"}</strong><small>RESEARCH MODE</small></span>
      </div>
    </section>
  )
}

function HomeDashboard({
  analysis,
  assistantState,
  command,
  connection,
  conversations,
  lastQuery,
  readiness,
  voiceEnabled,
  voiceError,
  voiceSupported,
  onAnalysis,
  onCommandChange,
  onStateChange,
  onStopSpeaking,
  onToggleVoice,
}: HomeDashboardProps) {
  return (
    <div className="friday-home">
      <section className="friday-hero" aria-label="FRIDAY assistant">
        <AICore
          connection={connection}
          readiness={readiness}
          analysis={analysis}
          assistantState={assistantState}
        />

        <CommandBar
          assistantState={assistantState}
          command={command}
          onCommandChange={onCommandChange}
          onAnalysis={onAnalysis}
          onStateChange={onStateChange}
          onStopSpeaking={onStopSpeaking}
          onToggleVoice={onToggleVoice}
          voiceEnabled={voiceEnabled}
          voiceError={voiceError}
          voiceSupported={voiceSupported}
        />

        <div className="quick-actions" aria-label="Quick command starters">
          {quickActions.map((action) => (
            <button type="button" key={action.label} onClick={() => onCommandChange(action.command)}>
              <span>{action.code}</span>
              <div><strong>{action.label}</strong><small>{action.detail}</small></div>
              <i aria-hidden="true">→</i>
            </button>
          ))}
        </div>
      </section>

      <section className="home-dashboard-grid" aria-label="Trading intelligence overview">
        <IntelligenceOverview analysis={analysis} />
        <ConversationPanel state={assistantState} query={lastQuery} analysis={analysis} />

        <section className="home-card market-overview" aria-labelledby="market-overview-title">
          <div className="home-card-heading">
            <div>
              <span className="eyebrow">MARKETS &amp; WATCHLIST</span>
              <h2 id="market-overview-title">REFERENCE OVERVIEW</h2>
            </div>
            <span>NO LIVE POLLING</span>
          </div>
          <p>Browse exchange-aware catalogue records without loading live prices or embedded charts on Home.</p>
          <div className="market-overview-actions">
            <a href="#markets">Open instrument catalogue <span>→</span></a>
            <small>Quote and historical-data access remain unverified.</small>
          </div>
        </section>

        <ActivityLog connection={connection} readiness={readiness} analysis={analysis} />
        <SpecialistActivity analysis={analysis} />
        <RecentConversations conversations={conversations} />
      </section>

      <details className="agent-network-drawer">
        <summary>
          <span><b>09</b> Specialist roster</span>
          <small>Expand the complete agent network</small>
        </summary>
        <AgentPanel />
      </details>
    </div>
  )
}

export default HomeDashboard
