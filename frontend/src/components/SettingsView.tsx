import { useEffect, useMemo, useState } from "react"

import {
  getLLMBudget,
  getLLMUsage,
  type LLMBudgetStatus,
  type LLMUsageRecord,
} from "../services/api"
import { formatInr, providerHealthLabel, summarizeProviderUsage } from "./budgetPresentation"
import "./SettingsView.css"

type LoadState = "loading" | "ready" | "error"

export default function SettingsView({ refreshKey }: { refreshKey: number }) {
  const [loadState, setLoadState] = useState<LoadState>("loading")
  const [budget, setBudget] = useState<LLMBudgetStatus | null>(null)
  const [usage, setUsage] = useState<LLMUsageRecord[]>([])
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let mounted = true

    Promise.all([
      getLLMBudget(controller.signal),
      getLLMUsage(controller.signal),
    ])
      .then(([budgetResponse, usageResponse]) => {
        if (!mounted) return
        setBudget(budgetResponse)
        setUsage(usageResponse)
        setLoadState("ready")
      })
      .catch((error: unknown) => {
        if (!mounted || (error instanceof DOMException && error.name === "AbortError")) return
        setLoadState("error")
      })

    return () => {
      mounted = false
      controller.abort()
    }
  }, [refreshKey, reloadKey])

  const completed = useMemo(
    () => usage.filter((record) => record.state === "completed"),
    [usage],
  )
  const pending = useMemo(
    () => usage.filter((record) => record.state === "reserved"),
    [usage],
  )
  const providerUsage = useMemo(() => summarizeProviderUsage(usage), [usage])

  return (
    <section className="settings-budget" aria-labelledby="settings-budget-title">
      <header className="settings-budget-header">
        <div>
          <span className="eyebrow">AI CONTROL PLANE / READ ONLY</span>
          <h2 id="settings-budget-title">AI Usage &amp; Budget</h2>
          <p>Local cost controls and sanitized provider usage. No billing controls are exposed here.</p>
        </div>
        <button type="button" onClick={() => {
          setLoadState("loading")
          setReloadKey((value) => value + 1)
        }} disabled={loadState === "loading"}>
          {loadState === "loading" ? "Refreshing…" : "Refresh data"}
        </button>
      </header>

      <div aria-live="polite">
        {loadState === "loading" && (
          <div className="budget-state-card" role="status">
            <span className="budget-spinner" aria-hidden="true" />
            Loading the local usage ledger…
          </div>
        )}

        {loadState === "error" && (
          <div className="budget-state-card budget-state-error" role="alert">
            <strong>Budget data unavailable</strong>
            <span>The backend ledger could not be read. No usage or pricing assumptions are shown.</span>
            <button type="button" onClick={() => {
              setLoadState("loading")
              setReloadKey((value) => value + 1)
            }}>Try again</button>
          </div>
        )}
      </div>

      {loadState === "ready" && budget && (
        <>
          <div className="budget-metric-grid">
            <Metric label="Monthly budget" value={formatInr(budget.monthly_budget_inr)} detail={`${formatInr(budget.usable_budget_inr)} usable`} />
            <Metric label="Monthly spent" value={formatInr(budget.monthly_spent_inr)} detail={`${formatInr(budget.monthly_committed_inr)} committed including pending`} tone="spent" />
            <Metric label="Remaining usable" value={formatInr(budget.remaining_usable_inr)} detail="Protected reserve excluded" tone="remaining" />
            <Metric label="Daily spent" value={formatInr(budget.daily_spent_inr)} detail={`${formatInr(budget.daily_committed_inr)} committed · ${formatInr(budget.daily_limit_inr)} limit`} />
            <Metric label="Protected reserve" value={formatInr(budget.protected_reserve_inr)} detail="Unavailable for ordinary requests" tone="reserve" />
          </div>

          <div className="budget-two-column">
            <article className="budget-panel">
              <div className="budget-panel-heading">
                <div>
                  <span className="eyebrow">PROVIDER POLICY</span>
                  <h3>Pricing &amp; authorization</h3>
                </div>
                <span className={`authorization-pill ${budget.paid_provider_authorized ? "authorized" : "disabled"}`}>
                  Paid providers {budget.paid_provider_authorized ? "authorized" : "disabled"}
                </span>
              </div>
              <p className="budget-panel-note">
                Per-request paid estimates are capped at {formatInr(budget.maximum_request_estimate_inr)}.
              </p>
              <div className="provider-pricing-list">
                {budget.provider_pricing.map((provider) => (
                  <div className="provider-pricing-row" key={provider.provider}>
                    <div>
                      <strong>{provider.provider}</strong>
                      <span>{provider.model ?? "No configured model"}</span>
                    </div>
                    <div className="provider-status-stack">
                      <span>{provider.configured ? "Configured" : "Not configured"}</span>
                      <b className={provider.pricing_status === "known" ? "known" : "unknown"}>
                        {provider.pricing_status === "known" ? `${provider.tier} pricing` : "Pricing unknown"}
                      </b>
                      <small>{providerHealthLabel(provider)}</small>
                    </div>
                  </div>
                ))}
              </div>
            </article>

            <article className="budget-panel">
              <div className="budget-panel-heading">
                <div>
                  <span className="eyebrow">CURRENT RESERVATIONS</span>
                  <h3>Pending requests</h3>
                </div>
                <span className="pending-count">{budget.pending_request_count}</span>
              </div>
              <p className="budget-panel-note">
                {formatInr(budget.pending_reserved_inr)} is currently reserved and not counted as spent.
              </p>
              {pending.length === 0 ? (
                <EmptyState text="No pending LLM cost reservations." />
              ) : (
                <div className="pending-list">
                  {pending.map((record) => (
                    <div key={record.request_id}>
                      <span>{record.provider} / {record.model}</span>
                      <strong>{formatInr(record.estimated_cost_inr)}</strong>
                      <small>{formatTimestamp(record.timestamp)}</small>
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>

          <article className="budget-panel provider-usage-panel">
            <div className="budget-panel-heading">
              <div>
                <span className="eyebrow">COMPLETED USAGE</span>
                <h3>Provider and model totals</h3>
              </div>
              <span className="record-count">{completed.length} completed</span>
            </div>
            {providerUsage.length === 0 ? (
              <EmptyState text="No completed AI provider usage has been recorded." />
            ) : (
              <div className="provider-summary-grid">
                {providerUsage.map((summary) => (
                  <div className="provider-summary" key={summary.key}>
                    <span>{summary.provider}</span>
                    <strong>{summary.model}</strong>
                    <b>{formatInr(summary.costInr)}</b>
                    <small>
                      {summary.requestCount} request{summary.requestCount === 1 ? "" : "s"}
                      {summary.estimatedOnlyCount > 0 ? ` · ${summary.estimatedOnlyCount} with token usage unknown` : ""}
                    </small>
                  </div>
                ))}
              </div>
            )}
          </article>

          <article className="budget-panel usage-ledger-panel">
            <div className="budget-panel-heading">
              <div>
                <span className="eyebrow">SANITIZED LEDGER</span>
                <h3>Recent usage</h3>
              </div>
              <span className="record-count">{usage.length} records</span>
            </div>
            {usage.length === 0 ? (
              <EmptyState text="The local AI usage ledger is empty." />
            ) : (
              <div className="usage-table-scroll">
                <table className="usage-table">
                  <thead><tr><th>Provider / model</th><th>Tokens</th><th>Estimated</th><th>Reconciled</th><th>Status</th><th>Time</th></tr></thead>
                  <tbody>
                    {usage.map((record) => (
                      <tr key={record.request_id}>
                        <td><strong>{record.provider}</strong><span>{record.model}</span></td>
                        <td>{tokenDisplay(record)}</td>
                        <td>{formatInr(record.estimated_cost_inr)}</td>
                        <td>{record.actual_cost_inr === null ? "Unknown" : formatInr(record.actual_cost_inr)}</td>
                        <td><span className={`ledger-state ${record.state}`}>{record.state}</span></td>
                        <td>{formatTimestamp(record.timestamp)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </article>
        </>
      )}
    </section>
  )
}

function Metric({ label, value, detail, tone = "" }: { label: string; value: string; detail: string; tone?: string }) {
  return <article className={`budget-metric ${tone}`}><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>
}

function EmptyState({ text }: { text: string }) {
  return <div className="budget-empty"><span aria-hidden="true">◇</span><p>{text}</p></div>
}

function tokenDisplay(record: LLMUsageRecord): string {
  if (record.input_tokens === null || record.output_tokens === null) {
    return `Unknown (≤ ${record.estimated_input_tokens + record.estimated_output_tokens} est.)`
  }
  return `${record.input_tokens} in / ${record.output_tokens} out`
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? "Unknown" : parsed.toLocaleString()
}
