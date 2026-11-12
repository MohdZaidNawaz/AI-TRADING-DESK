import assert from "node:assert/strict"
import test from "node:test"

import {
  amount,
  formatInr,
  providerHealthLabel,
  summarizeProviderUsage,
} from "../src/components/budgetPresentation.ts"
import type { LLMProviderPricingStatus, LLMUsageRecord } from "../src/services/api.ts"

function record(overrides: Partial<LLMUsageRecord> = {}): LLMUsageRecord {
  return {
    request_id: "00000000-0000-0000-0000-000000000001",
    provider: "gemini",
    model: "gemini-test",
    estimated_input_tokens: 100,
    estimated_output_tokens: 50,
    input_tokens: 80,
    output_tokens: 30,
    estimated_cost_inr: "0.50",
    actual_cost_inr: "0.35",
    state: "completed",
    timestamp: "2026-11-12T12:00:00+00:00",
    ...overrides,
  }
}

test("amount parsing is fail-closed for invalid or negative values", () => {
  assert.equal(amount("12.5"), 12.5)
  assert.equal(amount(0), 0)
  assert.equal(amount("not-a-number"), null)
  assert.equal(amount(-1), null)
  assert.equal(formatInr(null), "Unknown")
})

test("provider summaries exclude pending and released reservations", () => {
  const result = summarizeProviderUsage([
    record(),
    record({ request_id: "00000000-0000-0000-0000-000000000002", state: "reserved" }),
    record({ request_id: "00000000-0000-0000-0000-000000000003", state: "released" }),
  ])
  assert.equal(result.length, 1)
  assert.equal(result[0].requestCount, 1)
  assert.equal(result[0].costInr, 0.35)
})

test("missing provider token telemetry is identified as estimate-only", () => {
  const result = summarizeProviderUsage([
    record({ input_tokens: null, output_tokens: null, actual_cost_inr: null }),
  ])
  assert.equal(result[0].estimatedOnlyCount, 1)
  assert.equal(result[0].costInr, 0.5)
})

test("provider readiness labels preserve unknown quota and paid authorization states", () => {
  const free: LLMProviderPricingStatus = {
    provider: "gemini",
    model: "model",
    configured: true,
    pricing_status: "known",
    tier: "free",
    health: "unknown",
    eligible: true,
    paid_authorization_required: false,
    free_tier_quota_status: "unverified",
  }
  assert.equal(providerHealthLabel(free), "Free tier eligible · quota unverified")
  assert.equal(
    providerHealthLabel({
      ...free,
      provider: "openai",
      tier: "paid",
      eligible: false,
      paid_authorization_required: true,
      free_tier_quota_status: "not_applicable",
    }),
    "Paid authorization required",
  )
  assert.equal(
    providerHealthLabel({ ...free, health: "temporarily_unavailable" }),
    "Temporarily unavailable",
  )
})
