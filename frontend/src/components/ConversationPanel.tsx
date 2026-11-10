import type { AnalyzeResponse } from "../services/api"
import { ASSISTANT_STATE_LABELS } from "./assistantState"
import type { AssistantVisualState } from "./assistantState"

type ConversationPanelProps = {
  state: AssistantVisualState
  query: string | null
  analysis: AnalyzeResponse | null
}

const pendingCopy: Record<AssistantVisualState, string> = {
  idle: "Ask FRIDAY to coordinate a deterministic research pass.",
  listening: "Listening only while the microphone control remains pressed.",
  processing: "Converting the captured speech into command text.",
  "permission-denied": "Microphone permission was denied. Typed commands remain available.",
  thinking: "Structuring the request for the research desk.",
  analysing: "Coordinating the selected specialist routes.",
  speaking: "The latest desk response is ready.",
  error: "The request could not be completed. No execution action was taken.",
}

function ConversationPanel({ state, query, analysis }: ConversationPanelProps) {
  return (
    <section className={`conversation-panel conversation-${state}`} aria-labelledby="conversation-title">
      <div className="conversation-heading">
        <div>
          <span className="eyebrow">ASSISTANT CHANNEL</span>
          <h2 id="conversation-title">CONVERSATION</h2>
        </div>
        <span className="conversation-state"><i /> {ASSISTANT_STATE_LABELS[state]}</span>
      </div>

      <div className="conversation-body" aria-live="polite">
        {query && (
          <div className="conversation-turn conversation-user">
            <span>YOU</span>
            <p>{query}</p>
          </div>
        )}
        <div className="conversation-turn conversation-friday">
          <span>FRIDAY</span>
          <p>{analysis?.synthesis.final_response ?? pendingCopy[state]}</p>
        </div>
      </div>
    </section>
  )
}

export default ConversationPanel
