export const ANALYSIS_TIMEOUT_MS = 90_000
export const ANALYSIS_TIMEOUT_MESSAGE = "Analysis timed out after 90 seconds"

type ScheduleTimeout<THandle> = (
  callback: () => void,
  delayMilliseconds: number,
) => THandle

type ClearScheduledTimeout<THandle> = (handle: THandle) => void

export type AnalysisDeadline = {
  readonly timedOut: boolean
  clear: () => void
}

export function startAnalysisDeadline<THandle>(
  controller: AbortController,
  schedule: ScheduleTimeout<THandle>,
  clearScheduled: ClearScheduledTimeout<THandle>,
  timeoutMilliseconds = ANALYSIS_TIMEOUT_MS,
): AnalysisDeadline {
  let timedOut = false
  let cleared = false
  const handle = schedule(() => {
    if (cleared) return
    timedOut = true
    controller.abort()
  }, timeoutMilliseconds)

  return {
    get timedOut() {
      return timedOut
    },
    clear() {
      if (cleared) return
      cleared = true
      clearScheduled(handle)
    },
  }
}

export function analysisAbortMessage(timedOut: boolean): string | null {
  return timedOut ? ANALYSIS_TIMEOUT_MESSAGE : null
}
