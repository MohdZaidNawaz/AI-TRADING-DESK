import { useEffect, useState } from "react"
import type { FormEvent } from "react"
import type {
  AnalyzeResponse,
  AgentAnalysis,
  BacktestMetric,
  BacktestResponse,
  HistoricalDatasetMetadata,
  EconomicCalendarItem,
  NewsEconomicIntelligenceReport,
  MacroIntelligenceReport,
  LiquidityIntelligenceReport,
  RiskStrategyIntelligenceReport,
  TechnicalIndicator,
  TechnicalTimeframeAnalysis,
} from "../services/api"
import { getHistoricalDatasets, ingestHistoricalDataset, runHistoricalBacktest } from "../services/api"
import CommandBar from "./CommandBar"
import type { AssistantVisualState } from "./assistantState"

type MarketAnalysisViewProps = {
  analysis: AnalyzeResponse | null
  assistantState: AssistantVisualState
  command: string
  lastQuery: string | null
  voiceEnabled: boolean
  voiceError: string | null
  voiceSupported: boolean
  onAnalysis: (analysis: AnalyzeResponse, query: string) => boolean
  onCommandChange: (command: string) => void
  onStateChange: (state: AssistantVisualState) => void
  onStopSpeaking: () => void
  onToggleVoice: () => void
}

type ReportSection = {
  id: string
  eyebrow: string
  title: string
  agentIds: string[]
  unavailable: string
}

const requestStarters = [
  "Analyse XAUUSD.",
  "Analyse EURUSD on the 15-minute timeframe.",
  "Compare Gold and Silver.",
] as const

const reportSections: ReportSection[] = [
  {
    id: "technical",
    eyebrow: "PRICE / QUANT",
    title: "Technical Intelligence",
    agentIds: ["chartist_technician", "oracle_quant"],
    unavailable: "No technical or quantitative specialist result is present in the current response.",
  },
  {
    id: "liquidity",
    eyebrow: "POSITIONING / STRUCTURE",
    title: "Institutional & Liquidity Intelligence",
    agentIds: ["capitol_smart_money"],
    unavailable: "No validated OHLCV-derived liquidity report is present for this request.",
  },
  {
    id: "macro",
    eyebrow: "REGIME / RATES",
    title: "Macro Intelligence",
    agentIds: ["atlas_macro"],
    unavailable: "No macro specialist result is present in the current response.",
  },
  {
    id: "news",
    eyebrow: "CATALYST RECON",
    title: "News Intelligence",
    agentIds: ["scout_recon", "athena_analyst"],
    unavailable: "No reconnaissance or fundamental result is present. The current response does not expose source URLs or timestamps.",
  },
  {
    id: "risk",
    eyebrow: "LIMITS / INVALIDATION",
    title: "Risk Assessment",
    agentIds: ["sentinel_risk_officer"],
    unavailable: "No risk specialist result is present in the current response.",
  },
  {
    id: "strategy",
    eyebrow: "SCENARIOS / MEMORY",
    title: "Strategy Assessment",
    agentIds: ["ledger_the_book", "pilot_execution"],
    unavailable: "No research-memory or disabled execution-placeholder result is present in the current response.",
  },
]

function SpecialistResult({ result }: { result: AgentAnalysis }) {
  return (
    <article className={`report-agent-result result-${result.status}`}>
      <header>
        <div><span>{result.agent_name}</span><small>{result.agent_id.replaceAll("_", " ")}</small></div>
        <b>{result.status}</b>
      </header>
      <p>{result.summary}</p>
      {result.findings.length > 0 && (
        <ul>{result.findings.map((finding) => <li key={finding}>{finding}</li>)}</ul>
      )}
      <footer>
        <span>INTERPRETATION</span>
        <span>BACKEND CONFIDENCE {result.confidence.toFixed(2)}</span>
      </footer>
      {result.limitations.length > 0 && (
        <details>
          <summary>Limitations ({result.limitations.length})</summary>
          {result.limitations.map((limitation) => <p key={limitation}>{limitation}</p>)}
        </details>
      )}
    </article>
  )
}

function AnalysisSection({ section, analysis }: { section: ReportSection; analysis: AnalyzeResponse | null }) {
  const results = analysis?.agent_results.filter((result) => section.agentIds.includes(result.agent_id)) ?? []
  return (
    <section className={`analysis-report-panel report-${section.id}`} aria-labelledby={`report-${section.id}-title`}>
      <div className="phase2-panel-heading">
        <div><span>{section.eyebrow}</span><h2 id={`report-${section.id}-title`}>{section.title}</h2></div>
        <b>{results.length ? `${results.length} RESULT${results.length > 1 ? "S" : ""}` : "UNAVAILABLE"}</b>
      </div>
      {results.length ? (
        <div className="report-results">{results.map((result) => <SpecialistResult key={result.agent_id} result={result} />)}</div>
      ) : (
        <div className="phase2-empty-state"><span>NO SUPPORTED DATA</span><p>{section.unavailable}</p></div>
      )}
    </section>
  )
}

function indicatorName(indicator: TechnicalIndicator) {
  const parameters = indicator.parameters.map((item) => item.value).join("/")
  return `${indicator.feature_type.replaceAll("_", " ").toUpperCase()}${parameters ? ` ${parameters}` : ""}`
}

function indicatorValue(indicator: TechnicalIndicator) {
  if (indicator.status !== "available" || indicator.value === null) return indicator.status.replaceAll("_", " ")
  if (indicator.value_unit === "ratio") return `${(indicator.value * 100).toFixed(3)}%`
  return indicator.value.toLocaleString(undefined, { maximumFractionDigits: 5 })
}

function formatBacktestMetric(metric: BacktestMetric) {
  if (metric.status !== "available" || metric.value === null) return metric.status.replaceAll("_", " ")
  if (metric.unit.includes("percent")) return `${metric.value.toFixed(2)}%`
  if (metric.unit === "seconds") return `${(metric.value / 3600).toFixed(1)}h`
  return metric.value.toFixed(3)
}

function BacktestCurve({ response }: { response: BacktestResponse }) {
  const points = response.report.equity_curve
  if (!points.length) return <p className="backtest-no-chart">No completed trades; an equity curve cannot be calculated.</p>
  const values = points.map((point) => point.cumulative_return_pct)
  const minimum = Math.min(0, ...values)
  const maximum = Math.max(0, ...values)
  const span = maximum - minimum || 1
  const path = points.map((point, index) => {
    const x = points.length === 1 ? 50 : (index / (points.length - 1)) * 100
    const y = 90 - ((point.cumulative_return_pct - minimum) / span) * 80
    return `${x},${y}`
  }).join(" ")
  return (
    <svg className="backtest-equity-curve" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Actual cumulative trade return curve">
      <line x1="0" x2="100" y1={90 - ((0 - minimum) / span) * 80} y2={90 - ((0 - minimum) / span) * 80} />
      {points.length > 1 ? <polyline points={path} /> : <circle cx="50" cy={path.split(",")[1]} r="2" />}
    </svg>
  )
}

function HistoricalValidationPanel({ analysis }: { analysis: AnalyzeResponse | null }) {
  const suggestedSymbol = analysis?.routing.research_context?.primary_instrument.canonical_symbol ?? ""
  const suggestedTimeframe = analysis?.routing.research_context?.timeframe ?? "15m"
  const [symbol, setSymbol] = useState("")
  const [timeframe, setTimeframe] = useState("15m")
  const [startDate, setStartDate] = useState("")
  const [endDate, setEndDate] = useState("")
  const [response, setResponse] = useState<BacktestResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [datasets, setDatasets] = useState<HistoricalDatasetMetadata[]>([])
  const [selectedDatasetId, setSelectedDatasetId] = useState("")
  const [ingesting, setIngesting] = useState(false)
  const [datasetStatus, setDatasetStatus] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    getHistoricalDatasets(controller.signal)
      .then((result) => setDatasets(result.datasets))
      .catch(() => setDatasetStatus("Stored dataset metadata is currently unavailable."))
    return () => controller.abort()
  }, [])

  const requestDataset = async () => {
    const requestedSymbol = symbol.trim() || suggestedSymbol
    if (!requestedSymbol || !startDate || !endDate) {
      setDatasetStatus("Instrument, start date and end date are required for explicit ingestion.")
      return
    }
    setIngesting(true)
    setDatasetStatus(null)
    try {
      const result = await ingestHistoricalDataset({
        symbol: requestedSymbol,
        timeframe: timeframe || suggestedTimeframe,
        start_at: new Date(`${startDate}T00:00:00Z`).toISOString(),
        end_at: new Date(`${endDate}T23:59:59Z`).toISOString(),
      })
      setDatasetStatus(`${result.status.replaceAll("_", " ")}: ${result.reason_codes.join(" / ")}`)
      if (result.dataset) {
        const refreshed = await getHistoricalDatasets()
        setDatasets(refreshed.datasets)
        setSelectedDatasetId(result.dataset.dataset_id)
      }
    } catch (requestError) {
      setDatasetStatus(requestError instanceof Error ? requestError.message : "Historical ingestion failed.")
    } finally {
      setIngesting(false)
    }
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const requestedSymbol = symbol.trim() || suggestedSymbol
    if (!requestedSymbol) {
      setError("Select or enter an instrument before running historical validation.")
      return
    }
    setRunning(true)
    setError(null)
    try {
      const selectedDataset = datasets.find((item) => item.dataset_id === selectedDatasetId)
      const result = await runHistoricalBacktest({
        symbol: requestedSymbol,
        timeframe: timeframe || suggestedTimeframe,
        ...(startDate ? { start_at: new Date(`${startDate}T00:00:00Z`).toISOString() } : {}),
        ...(endDate ? { end_at: new Date(`${endDate}T23:59:59Z`).toISOString() } : {}),
        ...(selectedDataset ? {
          dataset_id: selectedDataset.dataset_id,
          dataset_fingerprint: selectedDataset.content_fingerprint,
        } : {}),
      })
      setResponse(result)
    } catch (requestError) {
      setResponse(null)
      setError(requestError instanceof Error ? requestError.message : "Historical validation failed.")
    } finally {
      setRunning(false)
    }
  }

  const report = response?.report
  return (
    <section className="historical-validation-panel" aria-labelledby="historical-validation-title">
      <div className="phase2-panel-heading">
        <div><span>08 / HISTORICAL EVIDENCE</span><h2 id="historical-validation-title">Strategy Validation</h2></div>
        <b>{running ? "EVALUATING" : report?.status.replaceAll("_", " ") ?? "NOT RUN"}</b>
      </div>
      <p className="backtest-intro">Explicitly retrieve a bounded immutable dataset, then evaluate the documented Phase 5.6 scenario against that exact evidence. Backtesting never fetches provider data, predicts returns, or authorizes execution.</p>
      <form className="backtest-controls" onSubmit={submit}>
        <label><span>Instrument</span><input value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder={suggestedSymbol || "e.g. XAU/USD"} /></label>
        <label><span>Timeframe</span><select value={timeframe} onChange={(event) => setTimeframe(event.target.value)}>{["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"].map((item) => <option key={item}>{item}</option>)}</select></label>
        <label><span>Start (optional)</span><input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label>
        <label><span>End (optional)</span><input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></label>
        <label className="dataset-selector"><span>Stored dataset</span><select value={selectedDatasetId} onChange={(event) => setSelectedDatasetId(event.target.value)}><option value="">Cached evidence (legacy)</option>{datasets.map((item) => <option key={item.dataset_id} value={item.dataset_id}>{item.canonical_symbol} · {item.timeframe} · {item.completeness} · {item.completed_bar_count} bars</option>)}</select></label>
        <button type="button" className="dataset-ingest-button" disabled={ingesting} onClick={requestDataset}>{ingesting ? "Request in progress..." : "Retrieve historical dataset"}</button>
        <button type="submit" disabled={running}>{running ? "Running validation..." : "Run historical validation"}</button>
      </form>
      {datasetStatus && <div className="dataset-status" role="status">{datasetStatus}</div>}
      {selectedDatasetId && (() => {
        const item = datasets.find((dataset) => dataset.dataset_id === selectedDatasetId)
        return item ? <div className={`dataset-summary dataset-${item.completeness}`}><span><b>{item.completeness}</b> validation state</span><span><b>{item.completed_bar_count}</b> completed</span><span><b>{item.provisional_bar_count}</b> provisional</span><span><b>{item.observed_gap_count}</b> observed gaps</span><span><b>{item.provider_id}</b> source</span><span><b>{item.venue ?? "unknown"}</b>{item.provider_identity_verified ? "verified identity" : "identity unverified"}</span><small>Coverage {new Date(item.coverage_start).toLocaleString()} to {new Date(item.coverage_end).toLocaleString()}. Missing trading intervals are not asserted without calendar evidence.</small></div> : null
      })()}
      {error && <div className="backtest-error" role="alert">{error}</div>}
      {report && (
        <div className="backtest-results">
          <div className="backtest-meta">
            <span><b>{report.symbol}</b> instrument</span>
            <span><b>{report.timeframe}</b> timeframe</span>
            <span><b>{report.data_quality.completed_bar_count}</b> completed candles</span>
            <span><b>{report.cost_assessment.status.replaceAll("_", " ")}</b> cost evidence</span>
          </div>
          <div className="backtest-metrics">
            <article><span>Total trades</span><b>{report.metrics.total_trades}</b></article>
            <article><span>Gross win rate</span><b>{formatBacktestMetric(report.metrics.gross.win_rate)}</b></article>
            <article><span>Gross total return</span><b>{formatBacktestMetric(report.metrics.gross.total_return)}</b></article>
            <article><span>Gross max drawdown</span><b>{formatBacktestMetric(report.metrics.gross.maximum_drawdown)}</b></article>
            <article><span>Net total return</span><b>{report.metrics.net ? formatBacktestMetric(report.metrics.net.total_return) : "unavailable"}</b></article>
            <article><span>Net max drawdown</span><b>{report.metrics.net ? formatBacktestMetric(report.metrics.net.maximum_drawdown) : "unavailable"}</b></article>
          </div>
          <div className="backtest-chart-wrap"><header><span>ACTUAL GROSS CUMULATIVE TRADE RETURN</span><small>gross returns</small></header><BacktestCurve response={response} /></div>
          <div className="backtest-window-grid">
            {report.validation_windows.map((window) => (
              <article key={window.window_id}><span>{window.kind.replaceAll("_", " ")}</span><b>{window.metrics.total_trades} completed trades</b><small>{window.observation_count} observations · gross return {formatBacktestMetric(window.metrics.gross.total_return)} · {window.incomplete_trade_count} incomplete</small></article>
            ))}
          </div>
          <div className="backtest-trade-history">
            <header><span>TRADE HISTORY</span><b>{report.trades.length}</b></header>
            {report.trades.length ? (
              <div className="backtest-table-scroll"><table><thead><tr><th>Signal</th><th>Side</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Gross</th><th>Net</th></tr></thead><tbody>{report.trades.map((trade) => <tr key={trade.trade_id}><td>{new Date(trade.signal_at).toLocaleString()}</td><td>{trade.direction}</td><td>{trade.entry_price}</td><td>{trade.exit_price}</td><td>{trade.exit_reason.replaceAll("_", " ")}</td><td>{trade.gross_return_pct.toFixed(2)}%</td><td>{trade.net_return_pct === null ? "unavailable" : `${trade.net_return_pct.toFixed(2)}%`}</td></tr>)}</tbody></table></div>
            ) : <p>No completed trades exist for this bounded sample and rule set.</p>}
          </div>
          {(report.warnings.length > 0 || report.limitations.length > 0) && <details className="backtest-limitations"><summary>Data quality, assumptions and limitations</summary>{report.warnings.map((item) => <p key={item}>Warning: {item}</p>)}{report.limitations.map((item) => <p key={item}>{item}</p>)}</details>}
        </div>
      )}
    </section>
  )
}

function TechnicalTimeframePanel({ item }: { item: TechnicalTimeframeAnalysis }) {
  const availableIndicators = item.indicators.filter((indicator) => indicator.status === "available")
  return (
    <article className="technical-timeframe-card">
      <header>
        <div>
          <span>{item.symbol} / {item.timeframe}</span>
          <h3 className={`trend-${item.trend_direction}`}>{item.trend_direction.replaceAll("_", " ")}</h3>
        </div>
        <b>{item.status}</b>
      </header>
      <div className="technical-provenance">
        <span>{item.provider_name}</span>
        <span>{item.completed_candle_count} completed / {item.provisional_candle_count} provisional</span>
        <span>{item.coverage_start ?? "No coverage"} to {item.coverage_end ?? "No coverage"}</span>
      </div>
      <div className="technical-indicator-grid">
        {item.indicators.map((indicator) => (
          <div key={indicator.feature_id} className={`technical-indicator status-${indicator.status}`}>
            <small>{indicatorName(indicator)}</small>
            <strong>{indicatorValue(indicator)}</strong>
            <span>{indicator.input_bar_count}/{indicator.required_bar_count} bars</span>
          </div>
        ))}
      </div>
      <div className="technical-structure-grid">
        <div><small>STRUCTURE</small><p>{item.structure_observations.join(" / ") || "No confirmed sequence"}</p></div>
        <div><small>SUPPORT CANDIDATES</small><p>{item.support_candidates.map((level) => level.price.toLocaleString()).join(" / ") || "Unavailable"}</p></div>
        <div><small>RESISTANCE CANDIDATES</small><p>{item.resistance_candidates.map((level) => level.price.toLocaleString()).join(" / ") || "Unavailable"}</p></div>
      </div>
      <footer>
        <span>{availableIndicators.length}/{item.indicators.length} indicators available</span>
        <span>{item.warnings.join(" / ") || "No calculation warnings"}</span>
      </footer>
      {item.limitations.length > 0 && (
        <details><summary>Limitations ({item.limitations.length})</summary>{item.limitations.map((value) => <p key={value}>{value}</p>)}</details>
      )}
    </article>
  )
}

function LiquidityIntelligencePanel({ report }: { report: LiquidityIntelligenceReport | null | undefined }) {
  return (
    <section className="technical-engine-panel" aria-labelledby="liquidity-engine-title">
      <div className="phase2-panel-heading">
        <div><span>03 / PRICE-ACTION LIQUIDITY</span><h2 id="liquidity-engine-title">Institutional Liquidity Intelligence</h2></div>
        <b>{report ? report.alignment.state.replaceAll("_", " ") : "UNAVAILABLE"}</b>
      </div>
      {!report?.analyses.length ? (
        <div className="phase2-empty-state">
          <span>NO VALIDATED LIQUIDITY SERIES</span>
          <p>BSL, SSL, sweeps, FVGs, BOS, CHoCH and order-block candidates remain unavailable until validated OHLCV evidence exists.</p>
        </div>
      ) : (
        <div className="technical-timeframe-list">
          {report.analyses.map((item) => (
            <article className="technical-timeframe-card" key={`${item.symbol}-${item.timeframe}-${item.provider_id}`}>
              <header>
                <div>
                  <span>{item.symbol} / {item.timeframe}</span>
                  <h3 className={`trend-${item.structure_trend}`}>{item.structure_trend.replaceAll("_", " ")}</h3>
                </div>
                <b>{item.status} / {item.freshness}</b>
              </header>
              <div className="technical-provenance">
                <span>{item.provider_name}</span>
                <span>{item.completed_candle_count} completed / {item.provisional_candle_count} provisional</span>
                <span>{item.coverage_start ?? "No coverage"} to {item.coverage_end ?? "No coverage"}</span>
              </div>
              <div className="technical-structure-grid">
                <div><small>BSL CANDIDATES</small><p>{item.liquidity_levels.filter((level) => level.side === "buy_side").map((level) => `${level.price.toLocaleString()} · ${level.state}`).join(" / ") || "Unavailable"}</p></div>
                <div><small>SSL CANDIDATES</small><p>{item.liquidity_levels.filter((level) => level.side === "sell_side").map((level) => `${level.price.toLocaleString()} · ${level.state}`).join(" / ") || "Unavailable"}</p></div>
                <div><small>SWEEPS</small><p>{item.sweeps.map((event) => `${event.side.replaceAll("_", " ")} · ${event.confirmation}`).join(" / ") || "None confirmed"}</p></div>
                <div><small>FAIR VALUE GAPS</small><p>{item.fair_value_gaps.map((gap) => `${gap.direction} ${gap.lower_boundary.toLocaleString()}–${gap.upper_boundary.toLocaleString()} · ${gap.state}`).join(" / ") || "None detected"}</p></div>
                <div><small>BOS / CHoCH</small><p>{item.structure_breaks.map((event) => `${event.kind.replaceAll("_", " ")} · ${event.confirmation}`).join(" / ") || "None detected"}</p></div>
                <div><small>ORDER-BLOCK CANDIDATES</small><p>{item.order_block_candidates.map((block) => `${block.direction} ${block.lower_boundary.toLocaleString()}–${block.upper_boundary.toLocaleString()} · ${block.state}`).join(" / ") || "None detected"}</p></div>
              </div>
              <footer>
                <span>{item.structure_points.length} confirmed / {item.provisional_structure_points.length} provisional pivots</span>
                <span>{item.warnings.join(" / ") || "No calculation warnings"}</span>
              </footer>
              <details>
                <summary>Evidence and limitations</summary>
                <p>Evaluated {item.evaluated_at}; retrieved {item.retrieved_at}; provider {item.provider_id}.</p>
                {item.limitations.map((value) => <p key={value}>{value}</p>)}
              </details>
            </article>
          ))}
        </div>
      )}
      {report && (
        <p className="conclusion-empty">
          These are deterministic price-action candidates. FRIDAY did not observe institutional orders or generate execution authority.
        </p>
      )}
    </section>
  )
}

function RiskStrategyPanel({ report }: { report: RiskStrategyIntelligenceReport | null | undefined }) {
  return (
    <section className="technical-engine-panel" aria-labelledby="risk-strategy-title">
      <div className="phase2-panel-heading">
        <div><span>06 / CONDITIONAL DECISION SUPPORT</span><h2 id="risk-strategy-title">Risk & Strategy Intelligence</h2></div>
        <b>{report?.status.replaceAll("_", " ") ?? "UNAVAILABLE"}</b>
      </div>
      {!report ? (
        <div className="phase2-empty-state">
          <span>NO DETERMINISTIC RISK REPORT</span>
          <p>Entry, invalidation, volatility, sizing and strategy quality remain unavailable until validated evidence is assessed.</p>
        </div>
      ) : (
        <>
          <div className="technical-provenance">
            <span>Volatility {report.volatility.regime}</span>
            <span>ATR {report.volatility.atr?.toLocaleString() ?? "unavailable"}</span>
            <span>Realized volatility {report.volatility.realized_volatility?.toFixed(6) ?? "unavailable"}</span>
            <span>Research only / no selected scenario</span>
          </div>
          <div className="technical-timeframe-list">
            {report.scenarios.map((scenario) => (
              <article className="technical-timeframe-card" key={scenario.direction}>
                <header>
                  <div><span>{scenario.direction.toUpperCase()} SCENARIO</span><h3 className={`trend-${scenario.direction}`}>{scenario.status.replaceAll("_", " ")}</h3></div>
                  <b>{scenario.quality.score.toFixed(1)}/100 · {scenario.quality.sufficiency}</b>
                </header>
                <div className="technical-structure-grid">
                  <div><small>ENTRY ZONE</small><p>{scenario.entry_zone ? `${scenario.entry_zone.lower.toLocaleString()} – ${scenario.entry_zone.upper.toLocaleString()} (${scenario.entry_zone.basis.replaceAll("_", " ")})` : "Unavailable"}</p></div>
                  <div><small>INVALIDATION / STOP</small><p>{scenario.stop_loss_candidate?.toLocaleString() ?? "Unavailable"}</p></div>
                  <div><small>TARGET CANDIDATES</small><p>{scenario.target_candidates.map((value) => value.toLocaleString()).join(" / ") || "Unavailable"}</p></div>
                  <div><small>RISK / REWARD</small><p>{scenario.risk_reward.ratio?.toFixed(2) ?? "Unavailable"}</p></div>
                  <div><small>STOP / VOLATILITY</small><p>{scenario.risk_reward.stop_suitability}</p></div>
                  <div><small>POSITION SIZE</small><p>{scenario.position_sizing.position_size !== null ? `${scenario.position_sizing.position_size} ${scenario.position_sizing.size_unit}` : scenario.position_sizing.status}</p></div>
                  <div><small>ACCOUNT RISK</small><p>{scenario.position_sizing.risk_percentage !== null ? `${scenario.position_sizing.risk_percentage}% / ${scenario.position_sizing.risk_amount ?? "unknown"} ${scenario.position_sizing.account_currency ?? ""}` : "Verified account input unavailable"}</p></div>
                </div>
                <details>
                  <summary>Evidence, conflicts and quality factors</summary>
                  <p><b>Supporting:</b> {scenario.supporting_evidence.join(" / ") || "None established"}</p>
                  <p><b>Conflicting:</b> {scenario.conflicting_evidence.join(" / ") || "None recorded"}</p>
                  <p><b>Missing:</b> {scenario.missing_evidence.join(" / ") || "None"}</p>
                  {scenario.quality.factors.map((factor) => <p key={factor.factor}>{factor.factor.replaceAll("_", " ")}: {factor.points.toFixed(1)}/{factor.weight} · {factor.reason}</p>)}
                  {scenario.limitations.map((value) => <p key={value}>{value}</p>)}
                </details>
              </article>
            ))}
          </div>
          <p className="conclusion-empty">Quality is deterministic evidence completeness, not win probability. No scenario is selected and no execution authority exists.</p>
        </>
      )}
    </section>
  )
}

function economicValue(value: EconomicCalendarItem["actual"]) {
  return value ? `${value.value} ${value.scale} ${value.unit}` : "Unavailable"
}

function NewsEconomicPanel({ report }: { report: NewsEconomicIntelligenceReport | null | undefined }) {
  return (
    <section className="news-economic-panel" aria-labelledby="news-economic-title">
      <div className="phase2-panel-heading">
        <div><span>03 / VERIFIED CATALYST CONTEXT</span><h2 id="news-economic-title">News & Economic Intelligence</h2></div>
        <b>{report ? report.news_retrieval.status.replaceAll("_", " ") : "UNAVAILABLE"}</b>
      </div>
      {!report ? (
        <div className="phase2-empty-state"><span>NO NEWS OR CALENDAR REPORT</span><p>No supported retrieval was run for this instrument, and missing evidence was not invented.</p></div>
      ) : (
        <div className="news-economic-grid">
          <div className="intelligence-column">
            <header><span>RELEVANT FINANCIAL NEWS</span><b>{report.relevant_news.length}</b></header>
            <div className="news-provider-statuses">
              {report.news_provider_statuses.map((provider) => (
                <small key={provider.provider_id}>
                  {provider.provider_name} · {provider.status.replaceAll("_", " ")} · {provider.request_count} request(s)
                  {` · ${provider.cooldown_status.replaceAll("_", " ")} · ${provider.cache_status.replaceAll("_", " ")}`}
                </small>
              ))}
            </div>
            {report.relevant_news.length ? report.relevant_news.map((item) => (
              <article className={`news-evidence-card freshness-${item.freshness}`} key={item.evidence_id}>
                <div><span>{item.provider_name} · {item.publisher_domain ?? "publisher unknown"}</span><b>{item.evidence_origin.replaceAll("_", " ")} · {item.freshness}</b></div>
                <h3>{item.headline}</h3>
                {item.summary && <p>{item.summary}</p>}
                <footer>
                  {item.seen_at && <span>GDELT seen <time dateTime={item.seen_at}>{item.seen_at}</time></span>}
                  {item.published_at ? (
                    <span>Published <time dateTime={item.published_at}>{item.published_at}</time></span>
                  ) : <span>Original publication time unavailable</span>}
                  <span>{item.relevance_reasons.join(" / ")}</span>
                  {item.search_category && <span>{item.search_category}</span>}
                  <span>{[item.language, item.source_country].filter(Boolean).join(" / ") || "locale unknown"}</span>
                  <a href={item.url} target="_blank" rel="noreferrer">SOURCE</a>
                </footer>
              </article>
            )) : <p className="intelligence-unavailable">No validated relevant article is available.</p>}
          </div>
          <div className="intelligence-column">
            <header><span>ECONOMIC CALENDAR</span><b>{report.calendar_provider_status.replaceAll("_", " ")}</b></header>
            {[...report.upcoming_events, ...report.released_events].length ? [...report.upcoming_events, ...report.released_events].map((item) => (
              <article className="calendar-evidence-card" key={item.evidence_id}>
                <div><span>{item.currency ?? item.country ?? "REGION UNKNOWN"}</span><b>{item.impact} impact</b></div>
                <h3>{item.event_name}</h3>
                <p>{item.status} · {item.scheduled_at} · evidence {item.freshness}</p>
                <dl>
                  <div><dt>Previous</dt><dd>{economicValue(item.previous)}</dd></div>
                  <div><dt>Forecast</dt><dd>{economicValue(item.forecast)}</dd></div>
                  <div><dt>Actual</dt><dd>{economicValue(item.actual)}</dd></div>
                </dl>
                <small>{item.provider_name} · retrieved {item.retrieved_at}</small>
              </article>
            )) : <p className="intelligence-unavailable">No configured economic-calendar provider is available. Event times and values remain unknown.</p>}
          </div>
          <div className="macro-intelligence-column">
            <header><span>DETERMINISTIC MACRO INTERPRETATION</span><b>{report.macro_findings.length}</b></header>
            {report.macro_findings.length ? report.macro_findings.map((item, index) => (
              <article key={`${item.evidence_ids.join("-")}-${item.category}-${index}`}>
                <span>{item.category.replaceAll("_", " ")}</span>
                <p><b>Verified fact.</b> {item.verified_fact}</p>
                <p><b>Interpretation.</b> {item.interpretation}</p>
                <p><b>Possible Gold implication.</b> {item.possible_gold_implication}</p>
                <small>{item.uncertainty}</small>
              </article>
            )) : <p className="intelligence-unavailable">No verified evidence supports a macro interpretation.</p>}
          </div>
          <div className="event-risk-column">
            <header><span>UPCOMING EVENT RISK</span><b>{report.event_risks.length}</b></header>
            {report.event_risks.map((item) => <p key={item.evidence_id}><b>{item.event_name}</b> · {item.scheduled_at} · {item.risk_statement}</p>)}
            {report.event_risks.length === 0 && <p className="intelligence-unavailable">No verified medium/high-impact upcoming event is available.</p>}
            {[...report.warnings, ...report.missing_information].map((item) => <small key={item}>{item}</small>)}
          </div>
        </div>
      )}
    </section>
  )
}

function MacroIntelligencePanel({ report }: { report: MacroIntelligenceReport | null | undefined }) {
  const facts = report?.instrument_assessments.flatMap((item) => item.verified_facts) ?? []
  return (
    <section className="news-economic-panel" aria-labelledby="macro-intelligence-title">
      <div className="phase2-panel-heading">
        <div><span>04 / OFFICIAL MACRO EVIDENCE</span><h2 id="macro-intelligence-title">Macro Intelligence</h2></div>
        <b>{facts.length ? `${facts.length} VERIFIED FACT${facts.length === 1 ? "" : "S"}` : "EVIDENCE UNAVAILABLE"}</b>
      </div>
      {!report ? (
        <div className="phase2-empty-state"><span>NO MACRO REPORT</span><p>No macro evidence assessment is present in this response.</p></div>
      ) : (
        <div className="news-economic-grid">
          <div className="intelligence-column">
            <header><span>PROVIDER AVAILABILITY</span><b>{report.provider_diagnostics.length}</b></header>
            {report.provider_diagnostics.map((provider) => (
              <article className="calendar-evidence-card" key={provider.provider_id ?? provider.reason_codes.join("-")}>
                <div><span>{provider.provider_id ?? "UNCONFIGURED"}</span><b>{provider.status.replaceAll("_", " ")}</b></div>
                <p>{provider.validated_count} validated / {provider.cached_evidence_count} cached</p>
                <small>{provider.reason_codes.join(" / ") || "No provider warning"}</small>
              </article>
            ))}
          </div>
          <div className="intelligence-column">
            <header><span>VERIFIED OBSERVATIONS</span><b>{facts.length}</b></header>
            {facts.length ? facts.map((fact) => <p key={fact}>{fact}</p>) : <p className="intelligence-unavailable">No fresh validated macro observation is available.</p>}
            {report.observations.map((item) => (
              <article className="calendar-evidence-card" key={item.observation_id}>
                <div><span>{item.indicator_id.replaceAll("_", " ")}</span><b>{item.freshness}</b></div>
                <p>{item.value.value} {item.value.unit} · {item.reference_period_start} to {item.reference_period_end}</p>
                <small>Retrieved via {item.retrieval_provider_name}; original publisher: {item.original_publisher_name ?? "unknown"}. Retrieved {item.retrieved_at}.</small>
              </article>
            ))}
            {report.indicator_changes.map((item) => (
              <article className="calendar-evidence-card" key={item.indicator_id}>
                <div><span>{item.indicator_id.replaceAll("_", " ")}</span><b>{item.status.replaceAll("_", " ")}</b></div>
                <p>{item.current ? `${item.current.value} ${item.current.unit} · ${item.direction ?? "direction unavailable"}` : "Current value unavailable"}</p>
                <small>{item.change ? `Change: ${item.change.value} ${item.change.unit} · ${item.previous_period_end ?? "unknown"} to ${item.current_period_end ?? "unknown"}` : item.limitations.join(" / ")}</small>
              </article>
            ))}
          </div>
          <div className="intelligence-column">
            <header><span>RELEASES / REVISIONS</span><b>{report.release_surprises.length + report.release_revisions.length}</b></header>
            {report.release_surprises.map((item, index) => (
              <article className="calendar-evidence-card" key={`surprise-${item.supporting_evidence_ids.join("-")}-${index}`}>
                <div><span>{item.indicator_id?.replaceAll("_", " ") ?? "ACTUAL / FORECAST"}</span><b>{item.status.replaceAll("_", " ")}</b></div>
                <p>{item.actual ? `${item.actual.value} ${item.actual.unit}` : "Actual unavailable"} / {item.forecast ? `${item.forecast.value} ${item.forecast.unit}` : "Forecast unavailable"}</p>
                <small>{item.surprise ? `Arithmetic surprise: ${item.surprise.value} ${item.surprise.unit} · ${item.direction ?? "direction unavailable"} · released ${item.released_at ?? "unknown"}` : item.limitations.join(" / ")}</small>
              </article>
            ))}
            {report.release_revisions.map((item, index) => (
              <article className="calendar-evidence-card" key={`revision-${item.supporting_evidence_ids.join("-")}-${index}`}>
                <div><span>PREVIOUS / REVISED</span><b>{item.status.replaceAll("_", " ")}</b></div>
                <p>{item.previous ? `${item.previous.value} ${item.previous.unit}` : "Previous unavailable"} / {item.revised_previous ? `${item.revised_previous.value} ${item.revised_previous.unit}` : "Revision unavailable"}</p>
                <small>{item.revision ? `Revision: ${item.revision.value} ${item.revision.unit} · ${item.direction ?? "direction unavailable"}` : item.limitations.join(" / ")}</small>
              </article>
            ))}
            {!report.release_surprises.length && !report.release_revisions.length && <p className="intelligence-unavailable">No validated actual/forecast or revision comparison is available.</p>}
          </div>
          <div className="macro-intelligence-column">
            <header><span>RATES / YIELD CONTEXT</span><b>{report.policy_rate_differentials.length + report.yield_curve_spreads.length + report.real_yield_contexts.length}</b></header>
            {report.policy_rate_differentials.map((item) => (
              <article key={`${item.base_currency}-${item.quote_currency}`}><span>{item.base_currency} / {item.quote_currency} POLICY</span><p>{item.differential ? `${item.differential.value} ${item.differential.unit}` : item.status}</p><small>{item.base_effective_at ?? "unknown"} / {item.quote_effective_at ?? "unknown"} · {item.limitations.join(" / ")}</small></article>
            ))}
            {report.yield_curve_spreads.map((item) => (
              <article key={`${item.short_maturity}-${item.long_maturity}`}><span>{item.short_maturity} / {item.long_maturity}</span><p>{item.spread ? `${item.spread.value} ${item.spread.unit}` : item.status}</p><small>{item.short_observed_at ?? "unknown"} / {item.long_observed_at ?? "unknown"} · {item.limitations.join(" / ")}</small></article>
            ))}
            {report.real_yield_contexts.map((item) => (
              <article key={item.maturity}><span>REAL YIELD {item.maturity}</span><p>{item.real_yield ? `${item.real_yield.value} ${item.real_yield.unit}` : item.status}</p><small>{item.limitations.join(" / ")}</small></article>
            ))}
            {!report.policy_rate_differentials.length && !report.yield_curve_spreads.length && !report.real_yield_contexts.length && <p className="intelligence-unavailable">Validated rates and yield evidence is unavailable.</p>}
          </div>
          <div className="event-risk-column">
            <header><span>MISSING / CONFLICTING EVIDENCE</span><b>{report.missing_information.length + report.instrument_assessments.reduce((count, item) => count + item.conflicting_evidence_ids.length, 0)}</b></header>
            {report.instrument_assessments.flatMap((item) => item.conditional_implications).map((item) => <small key={item}>Conditional: {item}</small>)}
            {report.instrument_assessments.flatMap((item) => item.conflicting_evidence_ids).map((item) => <small key={item}>Conflict evidence: {item}</small>)}
            {report.missing_information.slice(0, 12).map((item) => <small key={item}>{item}</small>)}
            {report.warnings.map((item) => <small key={item}>{item}</small>)}
          </div>
        </div>
      )}
    </section>
  )
}

function MarketAnalysisView({
  analysis,
  assistantState,
  command,
  lastQuery,
  voiceEnabled,
  voiceError,
  voiceSupported,
  onAnalysis,
  onCommandChange,
  onStateChange,
  onStopSpeaking,
  onToggleVoice,
}: MarketAnalysisViewProps) {
  const requestActive = assistantState === "thinking" || assistantState === "analysing"
  const researchContext = analysis?.routing.research_context
  const historical = researchContext?.evidence.historical_ohlcv

  return (
    <div className="phase2-workspace analysis-workspace">
      <header className="phase2-page-heading">
        <div>
          <span className="phase2-kicker">ADVANCED RESEARCH TERMINAL</span>
          <h1>Market Analysis Workspace</h1>
          <p>Run the existing deterministic specialist workflow and inspect only the evidence it returns.</p>
        </div>
        <div className={`phase2-mode mode-${requestActive ? "active" : analysis ? "ready" : "idle"}`}>
          <i />
          <span>{requestActive ? "ANALYSING" : analysis ? "REPORT AVAILABLE" : "AWAITING REQUEST"}</span>
        </div>
      </header>

      <section className="analysis-command-console" aria-label="Market analysis command">
        <div className="analysis-command-copy">
          <span>RESEARCH REQUEST</span>
          <h2>What should FRIDAY investigate?</h2>
          <p>Commands use the current `/api/analyze` contract. Unsupported data remains explicitly unavailable.</p>
        </div>
        <CommandBar
          assistantState={assistantState}
          command={command}
          onCommandChange={onCommandChange}
          onAnalysis={onAnalysis}
          onStateChange={onStateChange}
          onStopSpeaking={onStopSpeaking}
          onToggleVoice={onToggleVoice}
          voiceEnabled={voiceEnabled}
          voiceError={voiceError}
          voiceSupported={voiceSupported}
        />
        <div className="analysis-starters" aria-label="Example analysis commands">
          {requestStarters.map((starter) => (
            <button type="button" key={starter} onClick={() => onCommandChange(starter)}>{starter}</button>
          ))}
        </div>
      </section>

      <section className="analysis-summary-panel" aria-labelledby="executive-summary-title">
        <div className="phase2-panel-heading">
          <div><span>01 / EXECUTIVE VIEW</span><h2 id="executive-summary-title">Executive Summary</h2></div>
          <b>{analysis?.technical_analysis?.analyses.length ? "DETERMINISTIC OHLCV ANALYSIS" : analysis?.mode === "deterministic_mock" ? "DETERMINISTIC MOCK" : "NO ANALYSIS"}</b>
        </div>
        {analysis ? (
          <div className="executive-summary-grid">
            <div className="executive-primary">
              <small>CHIEF MARKET STRATEGIST</small>
              <p>{analysis.synthesis.final_response}</p>
              <span>{analysis.synthesis.caveat}</span>
            </div>
            <dl>
              <div><dt>Instrument</dt><dd>{researchContext?.primary_instrument.display_name ?? analysis.symbol ?? "Not identified"}</dd></div>
              <div><dt>Resolution</dt><dd>{researchContext?.primary_instrument.status ?? "Not supplied"}</dd></div>
              <div>
                <dt>Comparison instruments</dt>
                <dd>
                  {researchContext?.comparison_instruments.length
                    ? researchContext.comparison_instruments
                      .map((instrument) => `${instrument.display_name ?? instrument.canonical_symbol ?? instrument.expression} — ${instrument.status}`)
                      .join("; ")
                    : "None requested"}
                </dd>
              </div>
              <div><dt>Requested timeframe</dt><dd>{researchContext?.timeframe ?? "Unspecified"}</dd></div>
              <div><dt>Directional bias</dt><dd>Not supplied as a response field</dd></div>
              <div><dt>Specialists consulted</dt><dd>{analysis.synthesis.agents_consulted.length}</dd></div>
              <div><dt>Request</dt><dd>{lastQuery ?? analysis.query}</dd></div>
              <div><dt>Historical evidence</dt><dd>{historical?.status ?? "Unknown"}</dd></div>
              <div><dt>Data provider</dt><dd>{historical?.provider_id ?? "Not available"}</dd></div>
              <div><dt>Validated candles</dt><dd>{historical?.record_count ?? 0}</dd></div>
              <div>
                <dt>Historical coverage</dt>
                <dd>{historical?.coverage_start && historical.coverage_end ? `${historical.coverage_start} to ${historical.coverage_end}` : "Not available"}</dd>
              </div>
              <div><dt>Last source candle</dt><dd>{historical?.last_source_candle_at ?? "Not available"}</dd></div>
              <div><dt>Retrieved</dt><dd>{historical?.retrieved_at ?? "Not available"}</dd></div>
              <div><dt>Missing intervals</dt><dd>{historical?.missing_interval_count ?? 0}</dd></div>
              <div><dt>Provisional candles</dt><dd>{historical?.provisional_count ?? 0}</dd></div>
              <div>
                <dt>Provider diagnostic</dt>
                <dd>{historical?.error_category ?? (historical?.warning_codes.join(", ") || "None")}</dd>
              </div>
            </dl>
          </div>
        ) : (
          <div className="phase2-empty-state large-empty">
            <span>NO REPORT IN THIS SESSION</span>
            <p>Submit a supported request above. FRIDAY will organize the actual response without manufacturing missing market data.</p>
          </div>
        )}
      </section>

      <section className="technical-engine-panel" aria-labelledby="technical-engine-title">
        <div className="phase2-panel-heading">
          <div><span>02 / CALCULATED MARKET STRUCTURE</span><h2 id="technical-engine-title">Deterministic Technical Engine</h2></div>
          <b>{analysis?.technical_analysis ? "NO LLM / OHLCV DERIVED" : "UNAVAILABLE"}</b>
        </div>
        {analysis?.technical_analysis?.analyses.length ? (
          <div className="technical-timeframe-list">
            {analysis.technical_analysis.analyses.map((item) => <TechnicalTimeframePanel key={`${item.symbol}-${item.timeframe}-${item.provider_id}`} item={item} />)}
          </div>
        ) : (
          <div className="phase2-empty-state">
            <span>NO VALIDATED TECHNICAL SERIES</span>
            <p>Indicators and price structure remain unavailable until validated historical OHLCV evidence is present.</p>
          </div>
        )}
      </section>

      <LiquidityIntelligencePanel report={analysis?.liquidity_intelligence} />
      <RiskStrategyPanel report={analysis?.risk_strategy_intelligence} />
      <NewsEconomicPanel report={analysis?.news_intelligence} />
      <MacroIntelligencePanel report={analysis?.macro_intelligence} />

      <div className="analysis-report-grid">
        {reportSections.map((section) => <AnalysisSection key={section.id} section={section} analysis={analysis} />)}
      </div>

      <HistoricalValidationPanel analysis={analysis} />

      <section className="strategist-conclusion" aria-labelledby="strategist-conclusion-title">
        <div>
          <span>09 / FINAL SYNTHESIS</span>
          <h2 id="strategist-conclusion-title">Chief Market Strategist Conclusion</h2>
        </div>
        {analysis ? (
          <>
            <p>{analysis.synthesis.summary}</p>
            <blockquote>{analysis.synthesis.final_response}</blockquote>
            <div className="conclusion-evidence">
              <span><b>{analysis.synthesis.agents_consulted.length}</b> consulted specialists</span>
              <span><b>{analysis.coordination?.executions.filter((item) => item.state === "completed").length ?? 0}</b> completed results</span>
              <span><b>{analysis.coordination?.executions.filter((item) => item.state === "partial").length ?? 0}</b> partial results</span>
              <span><b>{analysis.coordination?.consensus?.replaceAll("_", " ") ?? "Unavailable"}</b> specialist consensus</span>
            </div>
            {(analysis.synthesis.disagreement_agent_ids?.length ?? 0) > 0 && (
              <div className="agent-limitations">
                <b>UNRESOLVED DISAGREEMENT</b>
                <p>{analysis.synthesis.disagreement_agent_ids?.join(" · ")}</p>
              </div>
            )}
            {(analysis.synthesis.missing_agent_ids?.length ?? 0) > 0 && (
              <div className="agent-limitations">
                <b>MISSING OR UNAVAILABLE SPECIALISTS</b>
                <p>{analysis.synthesis.missing_agent_ids?.join(" · ")}</p>
              </div>
            )}
            {(analysis.synthesis.conditional_scenarios?.length ?? 0) > 0 && (
              <div className="agent-limitations">
                <b>EVIDENCE-BOUND SCENARIOS</b>
                {analysis.synthesis.conditional_scenarios?.map((scenario) => <p key={scenario}>{scenario}</p>)}
              </div>
            )}
          </>
        ) : (
          <p className="conclusion-empty">No synthesis is available until a deterministic research request completes.</p>
        )}
      </section>
    </div>
  )
}

export default MarketAnalysisView
