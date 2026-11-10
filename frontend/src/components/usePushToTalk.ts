import { useEffect, useRef } from "react"

import type { AssistantVisualState } from "./assistantState"

type SpeechRecognitionAlternativeLike = {
  transcript: string
}

type SpeechRecognitionResultLike = {
  isFinal: boolean
  length: number
  [index: number]: SpeechRecognitionAlternativeLike
}

type SpeechRecognitionEventLike = Event & {
  resultIndex: number
  results: {
    length: number
    [index: number]: SpeechRecognitionResultLike
  }
}

type SpeechRecognitionErrorEventLike = Event & {
  error: string
}

type SpeechRecognitionLike = {
  continuous: boolean
  interimResults: boolean
  lang: string
  maxAlternatives: number
  onstart: (() => void) | null
  onresult: ((event: SpeechRecognitionEventLike) => void) | null
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
  abort: () => void
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionLike

type SpeechWindow = Window & {
  SpeechRecognition?: SpeechRecognitionConstructor
  webkitSpeechRecognition?: SpeechRecognitionConstructor
}

type PushToTalkOptions = {
  currentText: string
  onTextChange: (text: string) => void
  onStateChange: (state: AssistantVisualState) => void
  onMessageChange: (message: string) => void
}

const MAX_COMMAND_LENGTH = 500

function combinedTranscript(base: string, finalText: string, interimText: string): string {
  return [base, finalText, interimText]
    .map((value) => value.trim())
    .filter(Boolean)
    .join(" ")
    .slice(0, MAX_COMMAND_LENGTH)
}

export function usePushToTalk({
  currentText,
  onTextChange,
  onStateChange,
  onMessageChange,
}: PushToTalkOptions) {
  const recognition = useRef<SpeechRecognitionLike | null>(null)
  const recognitionStarted = useRef(false)
  const pressActive = useRef(false)
  const cancelled = useRef(false)
  const failed = useRef(false)
  const baseText = useRef("")
  const finalText = useRef("")
  const recognizedText = useRef("")
  const latestText = useRef(currentText)
  const textChangeCallback = useRef(onTextChange)
  const stateChangeCallback = useRef(onStateChange)
  const messageChangeCallback = useRef(onMessageChange)

  useEffect(() => {
    latestText.current = currentText
    textChangeCallback.current = onTextChange
    stateChangeCallback.current = onStateChange
    messageChangeCallback.current = onMessageChange
  }, [currentText, onMessageChange, onStateChange, onTextChange])

  const setStatus = (state: AssistantVisualState, message: string) => {
    stateChangeCallback.current(state)
    messageChangeCallback.current(message)
  }

  const cancel = () => {
    pressActive.current = false
    cancelled.current = true
    recognitionStarted.current = false
    const activeRecognition = recognition.current
    recognition.current = null
    activeRecognition?.abort()
    setStatus("idle", "Voice capture cancelled · no audio was retained")
  }

  useEffect(() => () => {
    pressActive.current = false
    cancelled.current = true
    recognition.current?.abort()
    recognition.current = null
  }, [])

  const start = () => {
    if (recognition.current !== null) return

    const speechWindow = window as SpeechWindow
    const Recognition = speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition
    if (!Recognition) {
      setStatus("error", "Speech recognition is not supported by this browser")
      return
    }

    pressActive.current = true
    cancelled.current = false
    failed.current = false
    recognitionStarted.current = false
    baseText.current = latestText.current.trim()
    finalText.current = ""
    recognizedText.current = baseText.current

    const activeRecognition = new Recognition()
    recognition.current = activeRecognition
    activeRecognition.continuous = true
    activeRecognition.interimResults = true
    activeRecognition.lang = navigator.language || "en-US"
    activeRecognition.maxAlternatives = 1

    activeRecognition.onstart = () => {
      recognitionStarted.current = true
      if (!pressActive.current) {
        setStatus("processing", "Processing captured speech")
        activeRecognition.stop()
        return
      }
      setStatus("listening", "Listening while you hold the microphone")
    }

    activeRecognition.onresult = (event) => {
      let interimText = ""
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index]
        const transcript = result[0]?.transcript ?? ""
        if (result.isFinal) finalText.current = `${finalText.current} ${transcript}`.trim()
        else interimText = `${interimText} ${transcript}`.trim()
      }
      recognizedText.current = combinedTranscript(
        baseText.current,
        finalText.current,
        interimText,
      )
      textChangeCallback.current(recognizedText.current)
    }

    activeRecognition.onerror = (event) => {
      failed.current = true
      recognitionStarted.current = false
      recognition.current = null
      if (cancelled.current && event.error === "aborted") return
      if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        setStatus("permission-denied", "Microphone permission was denied by the browser")
        return
      }
      if (event.error === "no-speech") {
        setStatus("error", "No speech was detected — hold the microphone and try again")
        return
      }
      setStatus("error", "Browser speech recognition could not complete")
    }

    activeRecognition.onend = () => {
      recognitionStarted.current = false
      if (recognition.current === activeRecognition) recognition.current = null
      if (failed.current || cancelled.current) return
      const transcript = recognizedText.current
      if (transcript !== baseText.current) {
        textChangeCallback.current(transcript)
        setStatus("idle", "Speech added to the command input")
      } else {
        setStatus("idle", "No speech was recognized")
      }
    }

    try {
      activeRecognition.start()
    } catch {
      recognition.current = null
      failed.current = true
      setStatus("error", "Microphone capture could not start")
    }
  }

  const stop = () => {
    pressActive.current = false
    cancelled.current = false
    const activeRecognition = recognition.current
    if (!activeRecognition) return
    setStatus("processing", "Processing captured speech")
    if (!recognitionStarted.current) return
    try {
      activeRecognition.stop()
    } catch {
      recognition.current = null
      failed.current = true
      setStatus("error", "Microphone capture could not stop cleanly")
    }
  }

  return { start, stop, cancel }
}
