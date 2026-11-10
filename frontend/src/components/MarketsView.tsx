import { useEffect, useState } from "react"
import type { FormEvent } from "react"

import { getInstrument, getInstruments } from "../services/api"
import { CATEGORY_LABELS, cataloguePageCount, catalogueState } from "../services/catalogue"
import type { CatalogueFilters, CatalogueResponse, InstrumentResponse } from "../services/catalogue"

const INITIAL_FILTERS: CatalogueFilters = { query: "", category: "", forexGroup: "", page: 1, limit: 25 }

function formatTimestamp(value: string | null): string {
  if (!value) return "Unknown"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString()
}

function InstrumentDetails({ instrumentId, onClose }: { instrumentId: string; onClose: () => void }) {
  const [response, setResponse] = useState<InstrumentResponse | null>(null)
  const [state, setState] = useState<"loading" | "ready" | "error">("loading")
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    getInstrument(instrumentId, controller.signal)
      .then((result) => {
        if (!active) return
        setResponse(result)
        setState("ready")
      })
      .catch(() => { if (active) setState("error") })
      .finally(() => window.clearTimeout(timeout))
    return () => {
      active = false
      controller.abort()
      window.clearTimeout(timeout)
    }
  }, [instrumentId, attempt])

  const instrument = response?.instrument
  return (
    <section className="instrument-details" aria-labelledby="instrument-details-title" aria-busy={state === "loading"}>
      <div className="catalogue-heading">
        <h3 id="instrument-details-title">Instrument details</h3>
        <button type="button" onClick={onClose}>Close details</button>
      </div>
      {state === "loading" && <p role="status">Loading instrument details…</p>}
      {state === "error" && (
        <div role="alert">
          <p>Instrument details unavailable. The record may have changed or the catalogue may be offline.</p>
          <button type="button" onClick={() => { setState("loading"); setAttempt((value) => value + 1) }}>Retry details</button>
        </div>
      )}
      {state === "ready" && instrument && (
        <>
          {response.is_stale && <p className="catalogue-warning" role="status">Stale cached instrument record.</p>}
          <dl>
            <dt>Symbol / name</dt><dd>{instrument.display_symbol} / {instrument.display_name ?? "Not supplied"}</dd>
            <dt>Exact provider symbol</dt><dd><code>{instrument.provider_symbol}</code></dd>
            <dt>Instrument ID</dt><dd><code>{instrument.instrument_id}</code></dd>
            <dt>Source</dt><dd>{instrument.source_id}</dd>
            <dt>Category</dt><dd>{CATEGORY_LABELS[instrument.category]}</dd>
            <dt>Base / quote currency</dt><dd>{instrument.base_currency ?? "Not supplied"} / {instrument.quote_currency ?? "Not supplied"}</dd>
            <dt>Forex group</dt><dd>{instrument.forex_group ?? "Not supplied"}</dd>
            <dt>Commodity category</dt><dd>{instrument.commodity_category ?? "Not supplied"}</dd>
            <dt>Available exchanges</dt><dd>{instrument.available_exchanges.length ? instrument.available_exchanges.join(", ") : "Not supplied"}</dd>
            <dt>Description</dt><dd>{instrument.description ?? "Not supplied"}</dd>
            <dt>Cache refreshed</dt><dd>{formatTimestamp(response.refreshed_at)}</dd>
            <dt>Quote access</dt><dd>Unknown — catalogue membership does not establish subscription access.</dd>
          </dl>
          <p>No exchange has been automatically selected. No quote or chart is requested.</p>
        </>
      )}
    </section>
  )
}

export default function MarketsView() {
  const [draft, setDraft] = useState(INITIAL_FILTERS)
  const [filters, setFilters] = useState(INITIAL_FILTERS)
  const [response, setResponse] = useState<CatalogueResponse | null>(null)
  const [state, setState] = useState<"loading" | "ready" | "error">("loading")
  const [attempt, setAttempt] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    getInstruments(filters, controller.signal)
      .then((result) => {
        if (!active) return
        setResponse(result)
        setState("ready")
      })
      .catch(() => { if (active) setState("error") })
      .finally(() => window.clearTimeout(timeout))
    return () => {
      active = false
      controller.abort()
      window.clearTimeout(timeout)
    }
  }, [filters, attempt])

  const load = (next: CatalogueFilters) => {
    setState("loading")
    setSelectedId(null)
    setFilters(next)
  }

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    load({ ...draft, query: draft.query.trim(), forexGroup: draft.category === "forex" ? draft.forexGroup.trim() : "", page: 1 })
  }

  const groups = [...new Set(response?.instruments.flatMap((item) => item.forex_group ? [item.forex_group] : []) ?? [])].sort()
  const pages = response ? cataloguePageCount(response.count, response.limit) : 1

  return (
    <section className="catalogue-view" aria-labelledby="markets-title">
      <span className="eyebrow">PROVIDER REFERENCE DIRECTORY</span>
      <h2 id="markets-title">Markets &amp; Watchlist</h2>
      <p>Explore the existing instrument catalogue. Reference records only — no live prices, charts or trading access.</p>

      <section className="watchlist-notice" aria-labelledby="watchlist-title">
        <h3 id="watchlist-title">Watchlist unavailable</h3>
        <p id="watchlist-reason">No watchlist backend is implemented. Add, remove and reorder are unavailable; nothing is saved locally.</p>
        <div className="catalogue-actions" aria-describedby="watchlist-reason">
          <button type="button" disabled>Add to watchlist</button>
          <button type="button" disabled>Remove</button>
          <button type="button" disabled>Reorder</button>
        </div>
      </section>

      <form className="catalogue-filters" onSubmit={submit}>
        <label>Symbol or name
          <input type="search" value={draft.query} maxLength={120} placeholder="e.g. EUR/USD or Bitcoin"
            onChange={(event) => setDraft({ ...draft, query: event.target.value })} />
        </label>
        <label>Category
          <select value={draft.category} onChange={(event) => setDraft({ ...draft, category: event.target.value as CatalogueFilters["category"], forexGroup: "" })}>
            <option value="">All categories</option>
            <option value="forex">Forex</option>
            <option value="commodity">Commodities</option>
            <option value="crypto">Crypto</option>
          </select>
        </label>
        {draft.category === "forex" && (
          <label>Forex group (exact value)
            <input value={draft.forexGroup} maxLength={120} list="forex-groups" placeholder="All groups"
              aria-describedby="forex-group-help" onChange={(event) => setDraft({ ...draft, forexGroup: event.target.value })} />
            <datalist id="forex-groups">{groups.map((group) => <option key={group} value={group} />)}</datalist>
            <small id="forex-group-help">Case-sensitive API value; suggestions are from the last loaded page only.</small>
          </label>
        )}
        <label>Results per page
          <select value={draft.limit} onChange={(event) => setDraft({ ...draft, limit: Number(event.target.value) })}>
            <option value={25}>25</option><option value={50}>50</option><option value={100}>100</option>
          </select>
        </label>
        <div className="catalogue-actions">
          <button type="submit">Search catalogue</button>
          <button type="button" onClick={() => { setDraft(INITIAL_FILTERS); load({ ...INITIAL_FILTERS }) }}>Reset</button>
        </div>
      </form>

      <div aria-busy={state === "loading"}>
        {state === "loading" && <p role="status">Loading catalogue…</p>}
        {state === "error" && (
          <div className="catalogue-warning" role="alert">
            <p>Catalogue unavailable or request timed out. No reference records can be shown for this request.</p>
            <button type="button" onClick={() => { setState("loading"); setAttempt((value) => value + 1) }}>Retry catalogue</button>
          </div>
        )}
        {state === "ready" && response && (
          <>
            <p role="status">
              {response.count} matching instruments · {response.is_stale ? "STALE CACHE" : "CACHE CURRENT"}
              {" · Refreshed: "}{formatTimestamp(response.refreshed_at)}
            </p>
            {response.is_stale && <p className="catalogue-warning">The backend is serving an older cached catalogue. Availability and metadata may have changed.</p>}
            {catalogueState(response) === "empty" ? <p>No instruments match these filters. Change the search or reset filters.</p> : (
              <div className="catalogue-table-scroll" tabIndex={0} role="region" aria-label="Instrument catalogue results">
                <table className="catalogue-table">
                  <caption>Catalogue results — select a symbol for full reference details</caption>
                  <thead><tr><th scope="col">Instrument</th><th scope="col">Provider symbol</th><th scope="col">Category / group</th><th scope="col">Exchanges</th></tr></thead>
                  <tbody>{response.instruments.map((instrument) => (
                    <tr key={instrument.instrument_id}>
                      <td><button type="button" aria-pressed={selectedId === instrument.instrument_id} onClick={() => setSelectedId(instrument.instrument_id)}>{instrument.display_symbol}</button><span>{instrument.display_name ?? "Name not supplied"}</span></td>
                      <td><code>{instrument.provider_symbol}</code></td>
                      <td>{CATEGORY_LABELS[instrument.category]}<span>{instrument.forex_group ?? instrument.commodity_category ?? "—"}</span></td>
                      <td>{instrument.available_exchanges.length ? instrument.available_exchanges.join(", ") : "Not supplied"}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            )}
            <nav className="catalogue-pagination" aria-label="Catalogue pagination">
              <button type="button" disabled={response.page <= 1} onClick={() => load({ ...filters, page: response.page - 1 })}>Previous</button>
              <span>Page {response.page} of {pages}</span>
              <button type="button" disabled={response.page >= pages} onClick={() => load({ ...filters, page: response.page + 1 })}>Next</button>
            </nav>
          </>
        )}
      </div>
      {selectedId && <InstrumentDetails key={selectedId} instrumentId={selectedId} onClose={() => setSelectedId(null)} />}
    </section>
  )
}