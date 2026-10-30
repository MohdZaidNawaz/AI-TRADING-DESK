import assert from "node:assert/strict"
import test from "node:test"

import { getAggregateLiveFreshness } from "../src/components/marketFreshness.ts"

test("stale takes precedence over delayed in a mixed live snapshot", () => {
  assert.equal(
    getAggregateLiveFreshness([
      { freshness: "live" },
      { freshness: "delayed" },
      { freshness: "stale" },
    ]),
    "stale",
  )
})

test("returns the uniform live freshness state", () => {
  assert.equal(
    getAggregateLiveFreshness([{ freshness: "live" }, { freshness: "live" }]),
    "live",
  )
})

test("returns the uniform delayed freshness state", () => {
  assert.equal(
    getAggregateLiveFreshness([
      { freshness: "delayed" },
      { freshness: "delayed" },
    ]),
    "delayed",
  )
})

test("returns the uniform stale freshness state", () => {
  assert.equal(
    getAggregateLiveFreshness([{ freshness: "stale" }, { freshness: "stale" }]),
    "stale",
  )
})
