import { useEffect, useRef, useState } from "react"

import type { AssistantVisualState } from "./assistantState"

type SpeechSynthesisOptions = {
  onStateChange: (state: AssistantVisualState) => void
}

function synthesisErrorMessage(error: string): string {
  if (error === "not-allowed") return "Speech playback was blocked by the browser"
  if (error === "audio-busy") return "Audio output is currently busy"
  if (error === "network") return "The browser speech service is unavailable"
  return "FRIDAY could not speak the response"
}

export function useSpeechSynthesis({ onStateChange }: SpeechSynthesisOptions) {
  const [enabled, setEnabled] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [supported] = useState(
    () => "speechSynthesis" in window && "SpeechSynthesisUtterance" in window,
  )
  const stateCallback = useRef(onStateChange)
  const activeUtterance = useRef<SpeechSynthesisUtterance | null>(null)
  const generation = useRef(0)

  useEffect(() => {
    stateCallback.current = onStateChange
  }, [onStateChange])

  const stop = (resetState = true) => {
    generation.current += 1
    activeUtterance.current = null
    setError(null)
    if (supported) window.speechSynthesis.cancel()
    if (resetState) stateCallback.current("idle")
  }

  useEffect(() => () => {
    generation.current += 1
    if (supported) window.speechSynthesis.cancel()
  }, [supported])

  const toggle = () => {
    if (!supported) {
      setError("Speech synthesis is not supported by this browser")
      stateCallback.current("error")
      return
    }
    setError(null)
    const next = !enabled
    if (!next) stop()
    setEnabled(next)
  }

  const speak = (text: string): boolean => {
    const response = text.trim()
    if (!enabled || !supported || !response) return false

    stop(false)
    setError(null)
    const currentGeneration = generation.current
    const utterance = new SpeechSynthesisUtterance(response)
    activeUtterance.current = utterance
    utterance.lang = navigator.language || "en-US"
    utterance.rate = 0.98
    utterance.pitch = 1
    utterance.volume = 1

    utterance.onstart = () => {
      if (generation.current !== currentGeneration) return
      stateCallback.current("speaking")
    }
    utterance.onend = () => {
      if (generation.current !== currentGeneration) return
      activeUtterance.current = null
      stateCallback.current("idle")
    }
    utterance.onerror = (event) => {
      if (generation.current !== currentGeneration) return
      activeUtterance.current = null
      setError(synthesisErrorMessage(event.error))
      stateCallback.current("error")
    }

    try {
      window.speechSynthesis.speak(utterance)
      return true
    } catch {
      activeUtterance.current = null
      setError("FRIDAY could not start speech playback")
      stateCallback.current("error")
      return false
    }
  }

  return {
    enabled,
    error,
    supported,
    speak,
    stop,
    toggle,
  }
}
