import assert from "node:assert/strict"
import test from "node:test"

import { cataloguePageCount, cataloguePath, catalogueState } from "../src/services/catalogue.ts"
import type { CatalogueFilters, CatalogueResponse } from "../src/services/catalogue.ts"
import { getInstrument, getInstruments } from "../src/services/api.ts"
import { NAVIGATION, screenFromHash } from "../src/services/navigation.ts"

const filters: CatalogueFilters = { query: "", category: "", forexGroup: "", page: 1, limit: 25 }
const empty: CatalogueResponse = { count: 0, page: 1, limit: 25, instruments: [], refreshed_at: null, is_stale: false }

test("navigation includes nine approved destinations and defaults safely to Home", () => {
  assert.deepEqual(NAVIGATION.map((item) => item.label), ["Home", "AI Intelligence", "Market Analysis", "Markets & Watchlist", "Trading Journal", "Recent Conversations", "Saved Analyses", "Economic Calendar", "Settings"])
  for (const item of NAVIGATION) assert.equal(screenFromHash(`#${item.id}`), item.id)
  assert.equal(screenFromHash(""), "home")
  assert.equal(screenFromHash("#unknown"), "home")
  assert.equal(screenFromHash("#screen-content"), "home")
})

test("catalogue query encodes symbol/name and exact forex group with pagination", () => {
  const url = new URL(cataloguePath({ ...filters, query: " EUR/USD & Euro ", category: "forex", forexGroup: " Major ", page: 3, limit: 50 }), "http://localhost")
  assert.equal(url.pathname, "/api/instruments")
  assert.equal(url.searchParams.get("q"), "EUR/USD & Euro")
  assert.equal(url.searchParams.get("forex_group"), "Major")
  assert.equal(url.searchParams.get("category"), "forex")
  assert.equal(url.searchParams.get("page"), "3")
  assert.equal(url.searchParams.get("limit"), "50")
})

test("all categories omit blank filters and non-forex categories omit forex group", () => {
  assert.equal(cataloguePath(filters), "/api/instruments?page=1&limit=25")
  for (const category of ["commodity", "crypto"] as const) {
    const path = cataloguePath({ ...filters, category, forexGroup: "Major" })
    assert.ok(path.includes(`category=${category}`))
    assert.ok(!path.includes("forex_group"))
  }
})

test("pagination handles zero, exact and partial result pages", () => {
  assert.equal(cataloguePageCount(0, 25), 1)
  assert.equal(cataloguePageCount(25, 25), 1)
  assert.equal(cataloguePageCount(26, 25), 2)
  assert.equal(cataloguePageCount(100, 25), 4)
})

test("catalogue distinguishes empty, stale and current records", () => {
  assert.equal(catalogueState(empty), "empty")
  const instrument = { instrument_id: "twelve_data:forex:EUR/USD" } as CatalogueResponse["instruments"][number]
  assert.equal(catalogueState({ ...empty, count: 1, instruments: [instrument] }), "ready")
  assert.equal(catalogueState({ ...empty, count: 1, instruments: [instrument], is_stale: true }), "stale")
})

test("typed catalogue client uses GET and passes cancellation without quote requests", async (context) => {
  const calls: { url: string; init?: RequestInit }[] = []
  context.mock.method(globalThis, "fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url, init })
    return new Response(JSON.stringify(empty), { headers: { "Content-Type": "application/json" } })
  })
  const controller = new AbortController()
  assert.deepEqual(await getInstruments(filters, controller.signal), empty)
  await getInstrument("twelve_data:crypto:BTC/USD", controller.signal)
  assert.equal(calls.length, 2)
  assert.ok(calls[0].url.endsWith(cataloguePath(filters)))
  assert.ok(calls[1].url.endsWith("/api/instruments/twelve_data%3Acrypto%3ABTC%2FUSD"))
  for (const call of calls) {
    assert.equal(call.init?.method, "GET")
    assert.equal(call.init?.signal, controller.signal)
  }
})

test("catalogue client rejects unavailable, missing detail and non-JSON responses", async (context) => {
  const mock = context.mock.method(globalThis, "fetch", async () => new Response(null, { status: 503 }))
  await assert.rejects(getInstruments(filters), /status 503/)
  mock.mock.mockImplementation(async () => new Response(null, { status: 404 }))
  await assert.rejects(getInstrument("missing"), /status 404/)
  mock.mock.mockImplementation(async () => new Response("<html>offline</html>", { headers: { "Content-Type": "text/html" } }))
  await assert.rejects(getInstruments(filters), /unexpected response format/)
})

test("catalogue client propagates cancellation", async (context) => {
  context.mock.method(globalThis, "fetch", async () => { throw new DOMException("Aborted", "AbortError") })
  await assert.rejects(getInstruments(filters, AbortSignal.abort()), { name: "AbortError" })
})