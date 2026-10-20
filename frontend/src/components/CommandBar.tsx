import { useRef, useState } from "react"
import type { FormEvent } from "react"

import { analyzeDeskQuery } from "../services/api"
import type { AnalyzeResponse } from "../services/api"

type CommandBarProps = {
  onAnalysis: (analysis: AnalyzeResponse) => void
}

type CommandState = "idle" | "submitting" | "complete" | "error"

function CommandBar({ onAnalysis }: CommandBarProps) {
  const [command, setCommand] = useState("")
  const [state, setState] = useState<CommandState>("idle")
  const [message, setMessage] = useState("Deterministic research only")
  const activeRequest = useRef<AbortController | null>(null)

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const query = command.trim()
    if (query.length < 3) {
      setState("error")
      setMessage("Enter at least 3 characters")
      return
    }

    activeRequest.current?.abort()
    const controller = new AbortController()
    activeRequest.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 8000)
    setState("submitting")
    setMessage("Running deterministic analysis")

    try {
      const result = await analyzeDeskQuery(query, controller.signal)
      onAnalysis(result)
      setState("complete")
      setMessage(`Completed · ${result.selected_agents.length} specialist routes`)
    } catch {
      setState("error")
      setMessage(controller.signal.aborted ? "Analysis timed out" : "Analysis API unavailable")
    } finally {
      window.clearTimeout(timeout)
      if (activeRequest.current === controller) activeRequest.current = null
    }
  }

  return (
    <footer className="command-dock">
      <form className="command-bar" onSubmit={submit}>
        <span className="command-symbol" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M8 9l3 3-3 3M13 15h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
        <label htmlFor="desk-command" className="sr-only">Run deterministic desk analysis</label>
        <input
          id="desk-command"
          value={command}
          onChange={(event) => setCommand(event.target.value)}
          placeholder="Ask the deterministic research desk..."
          autoComplete="off"
          minLength={3}
          maxLength={500}
          disabled={state === "submitting"}
        />
        <span className="command-hint"><kbd>↵</kbd></span>
        <button
          type="submit"
          className="send-command"
          aria-label="Run analysis"
          disabled={state === "submitting"}
        >
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M5 12h13M13 7l5 5-5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </form>
      <div className="dock-meta">
        <span><i /> LOCAL RESEARCH SESSION</span>
        <span>VERIFY ALL FINANCIAL DATA · NO EXECUTION</span>
        <span className={`command-result command-result-${state}`} aria-live="polite">{message}</span>
      </div>
    </footer>
  )
}

export default CommandBar
