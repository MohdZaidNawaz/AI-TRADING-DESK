import assert from "node:assert/strict"
import test from "node:test"

import {
  ANALYSIS_TIMEOUT_MESSAGE,
  ANALYSIS_TIMEOUT_MS,
  analysisAbortMessage,
  startAnalysisDeadline,
} from "../src/components/analysisDeadline.ts"

test("analysis deadline aborts only after the configured ninety-second timeout", () => {
  const controller = new AbortController()
  let scheduledDelay: number | null = null
  let scheduledCallback: (() => void) | null = null

  const deadline = startAnalysisDeadline(
    controller,
    (callback, delay) => {
      scheduledCallback = callback
      scheduledDelay = delay
      return 41
    },
    () => {},
  )

  assert.equal(ANALYSIS_TIMEOUT_MS, 90_000)
  assert.equal(scheduledDelay, 90_000)
  assert.equal(deadline.timedOut, false)
  assert.equal(controller.signal.aborted, false)

  assert.ok(scheduledCallback)
  scheduledCallback()

  assert.equal(deadline.timedOut, true)
  assert.equal(controller.signal.aborted, true)
  assert.equal(analysisAbortMessage(deadline.timedOut), ANALYSIS_TIMEOUT_MESSAGE)
})

test("ordinary cancellation remains distinct from a deadline timeout", () => {
  const controller = new AbortController()
  let clearedHandle: number | null = null

  const deadline = startAnalysisDeadline(
    controller,
    () => 73,
    (handle) => {
      clearedHandle = handle
    },
  )

  deadline.clear()
  controller.abort()

  assert.equal(clearedHandle, 73)
  assert.equal(controller.signal.aborted, true)
  assert.equal(deadline.timedOut, false)
  assert.equal(analysisAbortMessage(deadline.timedOut), null)
})

test("cleared deadline cannot later turn cancellation into a timeout", () => {
  const controller = new AbortController()
  let scheduledCallback: (() => void) | null = null
  let clearCount = 0

  const deadline = startAnalysisDeadline(
    controller,
    (callback) => {
      scheduledCallback = callback
      return 99
    },
    () => {
      clearCount += 1
    },
  )

  deadline.clear()
  deadline.clear()
  assert.ok(scheduledCallback)
  scheduledCallback()

  assert.equal(clearCount, 1)
  assert.equal(deadline.timedOut, false)
  assert.equal(controller.signal.aborted, false)
  assert.equal(analysisAbortMessage(false), null)
})
