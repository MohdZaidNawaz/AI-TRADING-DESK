export type AssistantVisualState =
  | "idle"
  | "listening"
  | "processing"
  | "permission-denied"
  | "thinking"
  | "analysing"
  | "speaking"
  | "error"

export const ASSISTANT_STATE_LABELS: Record<AssistantVisualState, string> = {
  idle: "READY",
  listening: "LISTENING",
  processing: "PROCESSING SPEECH",
  "permission-denied": "MICROPHONE DENIED",
  thinking: "THINKING",
  analysing: "ANALYSING",
  speaking: "RESPONDING",
  error: "ATTENTION REQUIRED",
}
