import ActivityLog from "./components/ActivityLog"
import AgentPanel from "./components/AgentPanel"
import AICore from "./components/AICore"
import CommandBar from "./components/CommandBar"
import Header from "./components/Header"
import MarketPanel from "./components/MarketPanel"
import "./App.css"

function App() {
  return (
    <div className="app-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />

      <Header />

      <main className="workspace">
        <div className="dashboard-grid">
          <AgentPanel />
          <AICore />
          <MarketPanel />
        </div>

        <ActivityLog />
      </main>

      <CommandBar />
    </div>
  )
}

export default App
