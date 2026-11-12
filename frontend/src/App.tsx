import { useEffect, useState } from "react"

import Header from "./components/Header"
import AIIntelligenceView from "./components/AIIntelligenceView"
import HomeDashboard from "./components/HomeDashboard"
import type { ConversationSummary } from "./components/HomeDashboard"
import MarketAnalysisView from "./components/MarketAnalysisView"
import { useSpeechSynthesis } from "./components/useSpeechSynthesis"
import MarketsView from "./components/MarketsView"
import Sidebar from "./components/Sidebar"
import SettingsView from "./components/SettingsView"
import { NAVIGATION, screenFromHash } from "./services/navigation"
import { getHealth, getProductionReadiness } from "./services/api"
import type {
  AnalyzeResponse,
  ApiConnectionState,
  ProductionReadinessReport,
} from "./services/api"
import type { AssistantVisualState } from "./components/assistantState"
import "./App.css"
import "./HomeDashboard.css"
import "./Phase2Workspace.css"

function App() {
  const [connection, setConnection] = useState<ApiConnectionState>("loading")
  const [readiness, setReadiness] = useState<ProductionReadinessReport | null>(null)
  const [analysis, setAnalysis] = useState<AnalyzeResponse | null>(null)
  const [lastQuery, setLastQuery] = useState<string | null>(null)
  const [assistantState, setAssistantState] = useState<AssistantVisualState>("idle")
  const [command, setCommand] = useState("")
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [screen, setScreen] = useState(() => screenFromHash(window.location.hash))
  const [budgetRefreshVersion, setBudgetRefreshVersion] = useState(0)
  const speech = useSpeechSynthesis({ onStateChange: setAssistantState })

  const handleAnalysis = (result: AnalyzeResponse, query: string) => {
    setAnalysis(result)
    setLastQuery(query)
    setBudgetRefreshVersion((value) => value + 1)
    setConversations((current) => [
      {
        id: result.analysis_id,
        query,
        response: result.synthesis.final_response,
        agentsConsulted: result.synthesis.agents_consulted.length,
      },
      ...current.filter((item) => item.id !== result.analysis_id),
    ].slice(0, 3))
    return speech.speak(result.synthesis.final_response)
  }

  useEffect(() => {
    const onHashChange = () => {
      setScreen(screenFromHash(window.location.hash))
      document.getElementById("screen-content")?.focus()
    }
    window.addEventListener("hashchange", onHashChange)
    return () => window.removeEventListener("hashchange", onHashChange)
  }, [])

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
    <div className="desk-layout">
      <a className="skip-link" href="#screen-content" onClick={(event) => {
        event.preventDefault()
        document.getElementById("screen-content")?.focus()
      }}>Skip to content</a>
      <Sidebar screen={screen} />
      <div className={`app-shell${screen === "home" ? " app-shell-home" : ""}`}>
        <div className="ambient ambient-one" />
        <div className="ambient ambient-two" />

        <Header connection={connection} readiness={readiness} />

        <main id="screen-content" className="screen-content" tabIndex={-1}>
          {screen === "home" && (
            <HomeDashboard
              analysis={analysis}
              assistantState={assistantState}
              command={command}
              connection={connection}
              conversations={conversations}
              lastQuery={lastQuery}
              readiness={readiness}
              voiceEnabled={speech.enabled}
              voiceError={speech.error}
              voiceSupported={speech.supported}
              onCommandChange={setCommand}
              onAnalysis={handleAnalysis}
              onStateChange={setAssistantState}
              onStopSpeaking={speech.stop}
              onToggleVoice={speech.toggle}
            />
          )}
          {screen === "ai-intelligence" && (
            <AIIntelligenceView
              analysis={analysis}
              assistantState={assistantState}
              currentCommand={command}
              lastQuery={lastQuery}
            />
          )}
          {screen === "market-analysis" && (
            <MarketAnalysisView
              analysis={analysis}
              assistantState={assistantState}
              command={command}
              lastQuery={lastQuery}
              voiceEnabled={speech.enabled}
              voiceError={speech.error}
              voiceSupported={speech.supported}
              onAnalysis={handleAnalysis}
              onCommandChange={setCommand}
              onStateChange={setAssistantState}
              onStopSpeaking={speech.stop}
              onToggleVoice={speech.toggle}
            />
          )}
          {screen === "markets" && <MarketsView />}
          {screen === "settings" && <SettingsView refreshKey={budgetRefreshVersion} />}
          {screen !== "home" && screen !== "ai-intelligence" && screen !== "market-analysis" && screen !== "markets" && screen !== "settings" && (
            <section className="catalogue-view" aria-labelledby="unavailable-title">
              <span className="eyebrow">NOT IMPLEMENTED</span>
              <h2 id="unavailable-title">{NAVIGATION.find((item) => item.id === screen)?.label}</h2>
              <p>This screen is not available yet. Existing research tools remain on Home.</p>
              <a href="#home">Return to Home</a>
            </section>
          )}
        </main>

      </div>
    </div>
  )
}

export { App }
