import { useEffect, useRef, useState } from "react"
import type { FormEvent } from "react"

import { analyzeDeskQuery } from "../services/api"
import type { AnalyzeResponse } from "../services/api"
import { ASSISTANT_STATE_LABELS } from "./assistantState"
import type { AssistantVisualState } from "./assistantState"
import { usePushToTalk } from "./usePushToTalk"

type CommandBarProps = {
  assistantState: AssistantVisualState
  command: string
  onCommandChange: (command: string) => void
  onAnalysis: (analysis: AnalyzeResponse, query: string) => boolean
  onStateChange: (state: AssistantVisualState) => void
  onStopSpeaking: () => void
  onToggleVoice: () => void
  voiceEnabled: boolean
  voiceError: string | null
  voiceSupported: boolean
}

function CommandBar({
  assistantState: state,
  command,
  onCommandChange,
  onAnalysis,
  onStateChange,
  onStopSpeaking,
  onToggleVoice,
  voiceEnabled,
  voiceError,
  voiceSupported,
}: CommandBarProps) {
  const [message, setMessage] = useState("Deterministic research only")
  const activeRequest = useRef<AbortController | null>(null)

  const transition = (nextState: AssistantVisualState) => {
    onStateChange(nextState)
  }

  const pushToTalk = usePushToTalk({
    currentText: command,
    onTextChange: onCommandChange,
    onStateChange: transition,
    onMessageChange: setMessage,
  })

  useEffect(() => () => {
    activeRequest.current?.abort()
  }, [])

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (state === "listening" || state === "processing") return
    onStopSpeaking()
    const query = command.trim()
    if (query.length < 3) {
      transition("error")
      setMessage("Enter at least 3 characters")
      return
    }

    activeRequest.current?.abort()
    const controller = new AbortController()
    activeRequest.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 8000)
    transition("thinking")
    setMessage("Structuring the research request")

    try {
      await new Promise((resolve) => window.setTimeout(resolve, 260))
      if (controller.signal.aborted) return
      transition("analysing")
      setMessage("Coordinating deterministic specialist routes")
      const result = await analyzeDeskQuery(query, controller.signal)
      const speechStarted = onAnalysis(result, query)
      setMessage(
        speechStarted
          ? `Speaking response · ${result.selected_agents.length} specialist routes`
          : `Response ready · ${result.selected_agents.length} specialist routes`,
      )
      if (!speechStarted) transition("idle")
    } catch {
      transition("error")
      setMessage(controller.signal.aborted ? "Analysis timed out" : "Analysis API unavailable")
    } finally {
      window.clearTimeout(timeout)
      if (activeRequest.current === controller) activeRequest.current = null
    }
  }

  const updateCommand = (value: string) => {
    onCommandChange(value)
    if (
      state === "speaking"
      || state === "error"
      || state === "permission-denied"
    ) {
      transition("idle")
      setMessage("Deterministic research only")
    }
  }

  const requestActive = state === "thinking" || state === "analysing"
  const voiceActive = state === "listening" || state === "processing"
  const commandBusy = requestActive || voiceActive
  const visibleMessage = voiceError ?? message

  return (
    <footer className={`command-dock command-dock-${state}`}>
      <form className="command-bar" onSubmit={submit} data-assistant-state={state}>
        <span className="command-symbol" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M8 9l3 3-3 3M13 15h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
        <label htmlFor="desk-command" className="sr-only">Run deterministic desk analysis</label>
        <input
          id="desk-command"
          value={command}
          onChange={(event) => updateCommand(event.target.value)}
          placeholder={
            state === "listening"
              ? "Listening while you hold the microphone..."
              : state === "processing"
                ? "Processing captured speech..."
                : "Ask FRIDAY to analyse a market, thesis or risk scenario..."
          }
          autoComplete="off"
          minLength={3}
          maxLength={500}
          disabled={requestActive}
          readOnly={voiceActive}
        />
        <button
          type="button"
          className="microphone-button"
          aria-label={state === "listening" ? "Release to stop listening" : "Hold to talk"}
          aria-pressed={state === "listening"}
          title="Hold to talk — audio is not recorded or retained"
          onPointerDown={(event) => {
            if (event.button !== 0 || requestActive || state === "processing") return
            event.preventDefault()
            event.currentTarget.setPointerCapture(event.pointerId)
            onStopSpeaking()
            pushToTalk.start()
          }}
          onPointerUp={(event) => {
            event.preventDefault()
            pushToTalk.stop()
            if (event.currentTarget.hasPointerCapture(event.pointerId)) {
              event.currentTarget.releasePointerCapture(event.pointerId)
            }
          }}
          onPointerCancel={() => pushToTalk.cancel()}
          onKeyDown={(event) => {
            if ((event.key === " " || event.key === "Enter") && !event.repeat) {
              event.preventDefault()
              pushToTalk.start()
            }
            if (event.key === "Escape") pushToTalk.cancel()
          }}
          onKeyUp={(event) => {
            if (event.key === " " || event.key === "Enter") {
              event.preventDefault()
              pushToTalk.stop()
            }
          }}
          onBlur={() => {
            if (state === "listening") pushToTalk.cancel()
          }}
          disabled={requestActive || state === "processing"}
        >
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M12 15.5a3.5 3.5 0 0 0 3.5-3.5V6a3.5 3.5 0 1 0-7 0v6a3.5 3.5 0 0 0 3.5 3.5Z" stroke="currentColor" strokeWidth="1.5" />
            <path d="M5.8 11.5a6.2 6.2 0 0 0 12.4 0M12 17.8V21M9.2 21h5.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
        <div className="speech-controls" aria-label="FRIDAY voice output controls">
          <button
            type="button"
            className="voice-toggle-button"
            aria-label={
              !voiceSupported
                ? "Speech synthesis unavailable in this browser"
                : voiceEnabled
                  ? "Disable spoken responses"
                  : "Enable spoken responses"
            }
            aria-pressed={voiceEnabled}
            title={voiceSupported ? "Toggle spoken FRIDAY responses" : "Speech synthesis is unavailable"}
            onClick={onToggleVoice}
            disabled={!voiceSupported}
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M5 10v4h3l4 3V7L8 10H5Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
              {voiceEnabled
                ? <path d="M15 9.2a4 4 0 0 1 0 5.6M17.5 6.8a7.4 7.4 0 0 1 0 10.4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                : <path d="m16 10 5 5m0-5-5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />}
            </svg>
            <span>{!voiceSupported ? "UNAVAILABLE" : voiceEnabled ? "VOICE ON" : "VOICE OFF"}</span>
          </button>
          <button
            type="button"
            className="stop-speaking-button"
            aria-label="Stop FRIDAY speaking"
            title="Stop spoken response"
            onClick={onStopSpeaking}
            disabled={state !== "speaking"}
          >
            <span aria-hidden="true" />
          </button>
        </div>
        <span className="command-hint"><kbd>↵</kbd></span>
        <button
          type="submit"
          className="send-command"
          aria-label="Run analysis"
          disabled={commandBusy}
        >
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M5 12h13M13 7l5 5-5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </form>
      <div className="dock-meta">
        <span className="dock-state"><i /> {ASSISTANT_STATE_LABELS[state]}</span>
        <span>VERIFY ALL FINANCIAL DATA · NO EXECUTION</span>
        <span className={`command-result command-result-${state}`} aria-live="polite">{visibleMessage}</span>
      </div>
    </footer>
  )
}

export default CommandBar
