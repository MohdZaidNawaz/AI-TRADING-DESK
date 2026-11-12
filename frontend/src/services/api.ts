import { cataloguePath } from "./catalogue.ts"
import type { CatalogueFilters, CatalogueResponse, InstrumentResponse } from "./catalogue.ts"

const API_BASE_URL = (
  import.meta.env?.VITE_API_BASE_URL || "http://127.0.0.1:8010"
).replace(/\/$/, "")

export type ApiConnectionState = "loading" | "online" | "error"

export type HealthResponse = {
  status: "ok"
  service: string
  version: string
}

export type Agent = {
  id: string
  name: string
  description: string
  status: "active" | "syncing" | "idle"
}

export type AgentListResponse = {
  count: number
  agents: Agent[]
}

export type LLMProviderPricingStatus = {
  provider: "openai" | "anthropic" | "gemini" | "groq"
  model: string | null
  configured: boolean
  pricing_status: "known" | "unknown"
  tier: "free" | "paid" | null
  health: "unknown" | "healthy" | "temporarily_unavailable" | "authentication_failed" | "configuration_error"
  eligible: boolean
  paid_authorization_required: boolean
  free_tier_quota_status: "not_applicable" | "unverified"
}

export type LLMBudgetStatus = {
  monthly_budget_inr: string | number
  usable_budget_inr: string | number
  protected_reserve_inr: string | number
  daily_limit_inr: string | number
  maximum_request_estimate_inr: string | number
  paid_provider_authorized: boolean
  monthly_committed_inr: string | number
  daily_committed_inr: string | number
  monthly_spent_inr: string | number
  daily_spent_inr: string | number
  pending_reserved_inr: string | number
  remaining_usable_inr: string | number
  pending_request_count: number
  provider_pricing: LLMProviderPricingStatus[]
}

export type LLMUsageRecord = {
  request_id: string
  provider: "openai" | "anthropic" | "gemini" | "groq"
  model: string
  estimated_input_tokens: number
  estimated_output_tokens: number
  input_tokens: number | null
  output_tokens: number | null
  estimated_cost_inr: string | number
  actual_cost_inr: string | number | null
  state: "reserved" | "completed" | "released"
  timestamp: string
}

export type Market = {
  symbol: string
  name: string
  price: number
  change_percent: number
  high: number
  low: number
  currency: string
  is_mock: boolean
  data_source?: "mock" | "twelve_data"
  quote_timestamp?: string | null
  received_at?: string | null
  quote_age_seconds?: number | null
  provider_is_delayed?: boolean | null
  freshness?: "live" | "delayed" | "stale" | "mock"
  freshness_reason?:
    | "fresh_quote"
    | "quote_age"
    | "provider_delayed"
    | "stale_quote_age"
    | "market_closed"
    | "mock"
  is_market_open?: boolean | null
}

export type MarketListResponse = {
  data_source: "mock" | "twelve_data"
  count: number
  markets: Market[]
}

export type ProductionReadinessCheck = {
  prerequisite: string
  status: string
  reason_codes: string[]
  source_reference: string | null
  checked_at: string
  authorizes_execution: false
}

export type ProductionReadinessReport = {
  schema_version: "production-authorization-readiness/v1"
  status: "blocked"
  evaluated_at: string
  checks: ProductionReadinessCheck[]
  reason_codes: string[]
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
  fingerprint: string
}

export type AgentAnalysis = {
  agent_id: string
  agent_name: string
  status: "completed" | "skipped" | "error"
  summary: string
  findings: string[]
  confidence: number
  limitations: string[]
  execution_state?: AgentExecutionState | null
  started_at?: string | null
  completed_at?: string | null
  evidence_references?: AgentEvidenceReference[]
  calculation_methods?: string[]
  missing_inputs?: string[]
  output_kinds?: AgentOutputKind[]
}

export type AgentExecutionState = "pending" | "running" | "completed" | "partial" | "failed" | "unavailable"
export type AgentOutputKind = "verified_observation" | "deterministic_calculation" | "interpretation" | "hypothesis" | "unavailable"

export type AgentEvidenceReference = {
  event_id: string
  source_id: string
  domain: string
  observed_at: string
  retrieved_at: string | null
  freshness: "fresh" | "stale" | "unknown"
  symbol: string | null
}

export type AgentCoordinationReport = {
  correlation_id: string
  planned_at: string
  completed_at: string
  tasks: Array<{
    task_id: string
    agent_id: string
    role: string
    selected: boolean
    dependency_agent_ids: string[]
  }>
  executions: Array<{
    task_id: string
    agent_id: string
    state: AgentExecutionState
    transitions: Array<{ state: AgentExecutionState; at: string }>
    started_at: string | null
    completed_at: string
    duration_ms: number
    dependency_agent_ids: string[]
    error_category: "agent_error" | "timeout" | "cancelled" | "dependency_unavailable" | null
    evidence_reference_count: number
    missing_inputs: string[]
    output_kinds: AgentOutputKind[]
  }>
  consensus: "agreement" | "conflicted" | "insufficient_data"
  agreement_agent_ids: string[]
  disagreement_agent_ids: string[]
  missing_agent_ids: string[]
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

export type AnalyzeResponse = {
  analysis_id: string
  mode: "deterministic_mock"
  symbol: string | null
  query: string
  selected_agents: string[]
  routing: {
    selected_agents: string[]
    reason: string
    research_context?: ResearchRequestContext
  }
  agent_results: AgentAnalysis[]
  synthesis: {
    status: "completed"
    agents_consulted: string[]
    summary: string
    final_response: string
    caveat: string
    agreement_agent_ids?: string[]
    disagreement_agent_ids?: string[]
    missing_agent_ids?: string[]
    evidence_references?: AgentEvidenceReference[]
    conditional_scenarios?: string[]
  }
  coordination?: AgentCoordinationReport | null
  technical_analysis?: TechnicalAnalysisReport | null
  news_intelligence?: NewsEconomicIntelligenceReport | null
  macro_intelligence?: MacroIntelligenceReport | null
  liquidity_intelligence?: LiquidityIntelligenceReport | null
  risk_strategy_intelligence?: RiskStrategyIntelligenceReport | null
}

export type RiskStrategyIntelligenceReport = {
  analysis_mode: "deterministic_risk_strategy_research"
  method_version: "risk-strategy/v1"
  subject: string
  evaluated_at: string
  status: "available" | "partial" | "stale" | "insufficient_data"
  volatility: {
    status: "available" | "unavailable" | "invalid"
    timeframe: string | null
    atr: number | null
    realized_volatility: number | null
    atr_percentage_of_price: number | null
    latest_range_to_atr: number | null
    regime: "low" | "normal" | "high" | "unknown"
    range_expansion: boolean | null
    stop_distance_atr_multiple: number | null
    stop_suitability: string
    warnings: string[]
  }
  scenarios: Array<{
    direction: "bullish" | "bearish" | "neutral"
    status: "conditional" | "no_trade" | "insufficient_data"
    entry_zone: {
      lower: number
      upper: number
      reference_price: number
      basis: string
      evidence_ids: string[]
    } | null
    entry_conditions: string[]
    invalidation_conditions: string[]
    stop_loss_candidate: number | null
    target_candidates: number[]
    risk_reward: {
      status: "available" | "unavailable" | "invalid"
      entry_price: number | null
      stop_loss: number | null
      invalidation_level: number | null
      target_price: number | null
      stop_distance_price: number | null
      stop_distance_pips: number | null
      stop_distance_atr_multiple: number | null
      stop_suitability: string
      potential_reward: number | null
      ratio: number | null
      limitations: string[]
    }
    position_sizing: {
      status: "available" | "unavailable" | "invalid"
      account_currency: string | null
      risk_percentage: number | null
      risk_amount: number | null
      position_size: number | null
      size_unit: "lots" | "contracts" | "units" | null
      units: number | null
      monetary_loss_per_size: number | null
      estimated_transaction_cost: number | null
      assumptions: string[]
      missing_inputs: string[]
      limitations: string[]
    }
    quality: {
      score: number
      maximum_score: 100
      sufficiency: "high" | "medium" | "low" | "insufficient"
      factors: Array<{
        factor: string
        weight: number
        score_fraction: number
        points: number
        reason: string
      }>
      interpretation: string
    }
    supporting_evidence: string[]
    conflicting_evidence: string[]
    missing_evidence: string[]
    expiration_conditions: string[]
    limitations: string[]
  }>
  selected_scenario: "none"
  warnings: string[]
  missing_inputs: string[]
  limitations: string[]
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

export type MacroProviderDiagnostic = {
  provider_id: string | null
  source_type: string
  status: "available" | "partial" | "not_configured" | "not_requested" | "unavailable" | "stale" | "rate_limited" | "timeout" | "network_error" | "malformed"
  requested_at: string | null
  retrieved_at: string | null
  request_count: number
  received_count: number
  validated_count: number
  reason_codes: string[]
  error_category: string | null
  failure_stage: string | null
  http_status_code: number | null
  request_sent: boolean | null
  retry_after_seconds: number | null
  cached_evidence_count: number
  new_evidence_count: number
}

export type MacroEconomicValue = {
  value: string
  unit: string
  scale: string
}

export type MacroAssessment = {
  status: "available" | "insufficient_data" | "invalid_input" | "incompatible" | "stale" | "not_available"
  method_id: string
  supporting_evidence_ids: string[]
  evidence_freshness: string[]
  missing_inputs: string[]
  warnings: string[]
  limitations: string[]
  confidence: number
}

export type MacroDirection = "above" | "below" | "in_line" | "rising" | "falling" | "unchanged"

export type MacroIntelligenceReport = {
  analysis_mode: "deterministic_macro_contract"
  subject: string
  evaluated_at: string
  provider_diagnostics: MacroProviderDiagnostic[]
  observations: {
    observation_id: string
    indicator_id: string
    value: MacroEconomicValue
    reference_period_start: string
    reference_period_end: string
    freshness: "fresh" | "aging" | "stale" | "unknown" | "not_available"
    published_at: string | null
    effective_at: string | null
    vintage_at: string | null
    retrieved_at: string
    retrieval_provider_id: string
    retrieval_provider_name: string
    original_publisher_id: string | null
    original_publisher_name: string | null
  }[]
  release_surprises: (MacroAssessment & { event_id: string | null; indicator_id: string | null; actual: MacroEconomicValue | null; forecast: MacroEconomicValue | null; surprise: MacroEconomicValue | null; direction: MacroDirection | null; released_at: string | null; retrieval_provider_id: string | null; original_publisher_id: string | null })[]
  release_revisions: (MacroAssessment & { event_id: string | null; indicator_id: string | null; previous: MacroEconomicValue | null; revised_previous: MacroEconomicValue | null; revision: MacroEconomicValue | null; direction: MacroDirection | null; released_at: string | null })[]
  indicator_changes: (MacroAssessment & { indicator_id: string; current: MacroEconomicValue | null; previous: MacroEconomicValue | null; change: MacroEconomicValue | null; direction: MacroDirection | null; previous_period_end: string | null; current_period_end: string | null })[]
  policy_rate_differentials: (MacroAssessment & { base_currency: string; quote_currency: string; base_rate: MacroEconomicValue | null; quote_rate: MacroEconomicValue | null; differential: MacroEconomicValue | null; base_effective_at: string | null; quote_effective_at: string | null })[]
  yield_curve_spreads: (MacroAssessment & { short_maturity: string; long_maturity: string; short_yield: MacroEconomicValue | null; long_yield: MacroEconomicValue | null; spread: MacroEconomicValue | null; short_observed_at: string | null; long_observed_at: string | null })[]
  real_yield_contexts: (MacroAssessment & { maturity: string; real_yield: MacroEconomicValue | null })[]
  cross_market_observations: (MacroAssessment & { primary_instrument_id: string; context_instrument_id: string; observation: string | null })[]
  instrument_assessments: (MacroAssessment & { instrument_id: string; instrument_class: string; assessed_factors: string[]; verified_facts: string[]; interpretations: string[]; conditional_implications: string[]; conflicting_evidence_ids: string[] })[]
  missing_information: string[]
  warnings: string[]
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

export type NewsRetrievalDiagnostic = {
  status: "available" | "partial" | "not_configured" | "not_requested" | "unsupported" | "rate_limited" | "timeout" | "network_error" | "authentication_error" | "invalid_request" | "server_error" | "provider_error" | "malformed"
  provider_id: string
  provider_name: string
  request_count: number
  search_queries: string[]
  requested_count: number
  received_count: number
  validated_count: number
  relevant_count: number
  duplicate_count: number
  stale_count: number
  retrieved_at: string | null
  evidence_ids: string[]
  reason_codes: string[]
  error_category: "configuration" | "network" | "authentication" | "invalid_request" | "rate_limit" | "timeout" | "server_error" | "malformed_response" | "provider_error" | null
  http_status_code: number | null
  http_status_class: "1xx" | "2xx" | "3xx" | "4xx" | "5xx" | null
  provider_reason_code: string | null
  failure_stage: "configuration" | "request" | "http_response" | "provider_response" | "response_decode" | "response_validation" | "provider_message" | "article_normalization" | "unknown" | null
  request_sent: boolean | null
  retry_after_seconds: number | null
  cooldown_status: "inactive" | "active" | "cooldown_unspecified"
  cooldown_until: string | null
  cache_status: "none" | "fresh_cache" | "stale_cache"
  cached_evidence_count: number
  newly_retrieved_evidence_count: number
  cached_evidence_ids: string[]
  new_evidence_ids: string[]
}

export type NewsIntelligenceItem = {
  evidence_id: string
  provider_id: string
  provider_name: string
  source_name: string
  headline: string
  url: string
  published_at: string | null
  seen_at: string | null
  event_at: string
  timestamp_basis: "original_publication" | "provider_seen" | "provider_event"
  retrieved_at: string
  freshness: "fresh" | "stale" | "future_dated" | "unknown"
  evidence_status: "validated"
  evidence_origin: "newly_retrieved" | "cached"
  symbols: string[]
  topics: string[]
  publisher_domain: string | null
  language: string | null
  source_country: string | null
  search_query: string | null
  search_category: string | null
  relevance_reasons: string[]
  summary: string | null
  sentiment_label: string | null
}

export type EconomicCalendarItem = {
  evidence_id: string
  occurrence_id: string
  event_name: string
  country: string | null
  currency: string | null
  scheduled_at: string
  released_at: string | null
  status: "scheduled" | "released" | "revised" | "delayed" | "cancelled" | "unknown"
  impact: "low" | "medium" | "high" | "unknown"
  previous: { value: string; unit: string; scale: string } | null
  forecast: { value: string; unit: string; scale: string } | null
  actual: { value: string; unit: string; scale: string } | null
  provider_id: string
  provider_name: string
  retrieved_at: string
  freshness: "fresh" | "stale" | "future_dated" | "unknown"
  evidence_status: "validated"
}

export type NewsEconomicIntelligenceReport = {
  analysis_mode: "deterministic_evidence"
  subject: string
  evaluated_at: string
  news_retrieval: NewsRetrievalDiagnostic
  news_provider_statuses: NewsRetrievalDiagnostic[]
  calendar_provider_status: "evidence_available" | "provider_unavailable"
  calendar_reason_codes: string[]
  calendar_error_category: "configuration" | null
  relevant_news: NewsIntelligenceItem[]
  upcoming_events: EconomicCalendarItem[]
  released_events: EconomicCalendarItem[]
  macro_findings: {
    category: string
    verified_fact: string
    interpretation: string
    possible_gold_implication: string
    evidence_ids: string[]
    confidence: "low" | "medium"
    uncertainty: string
  }[]
  event_risks: {
    evidence_id: string
    event_name: string
    scheduled_at: string
    impact: "low" | "medium" | "high" | "unknown"
    risk_statement: string
  }[]
  missing_information: string[]
  warnings: string[]
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

export type TechnicalIndicator = {
  feature_id: string
  feature_type: string
  status: "available" | "insufficient_data" | "undefined" | "rejected_input"
  value: number | null
  value_unit: "price" | "ratio" | "index"
  parameters: { name: string; value: number | string }[]
  method_id: string
  method_version: string
  input_bar_count: number
  required_bar_count: number
  quality_flags: string[]
  limitations: string[]
}

export type TechnicalLevel = {
  kind: "support" | "resistance"
  price: number
  occurred_at: string
  touches: 1
  basis: "swing_high" | "swing_low"
}

export type TechnicalTimeframeAnalysis = {
  status: "available" | "partial" | "insufficient_data"
  symbol: string
  asset_class: string
  venue: string | null
  timeframe: string
  provider_id: string
  provider_name: string
  retrieved_at: string
  coverage_start: string | null
  coverage_end: string | null
  validated_candle_count: number
  completed_candle_count: number
  provisional_candle_count: number
  missing_interval_count: number
  indicators: TechnicalIndicator[]
  trend_direction: "bullish" | "bearish" | "mixed" | "insufficient_data"
  trend_rule: string
  swing_window: number
  swing_highs: { occurred_at: string; price: number }[]
  swing_lows: { occurred_at: string; price: number }[]
  support_candidates: TechnicalLevel[]
  resistance_candidates: TechnicalLevel[]
  structure_observations: string[]
  warnings: string[]
  limitations: string[]
}

export type TechnicalAnalysisReport = {
  calculation_mode: "deterministic"
  method_version: "technical-analysis/v1"
  analyses: TechnicalTimeframeAnalysis[]
  available_timeframes: string[]
  unavailable_timeframes: string[]
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

export type LiquidityBarReference = {
  bar_id: string
  opened_at: string
  closed_at: string
  open: number
  high: number
  low: number
  close: number
  state: "completed" | "provisional"
}

export type LiquidityTimeframeAnalysis = {
  status: "available" | "partial" | "stale" | "insufficient_data"
  symbol: string
  asset_class: string
  venue: string | null
  timeframe: string
  provider_id: string
  provider_name: string
  evaluated_at: string
  retrieved_at: string
  coverage_start: string | null
  coverage_end: string | null
  freshness: "fresh" | "stale"
  completed_candle_count: number
  provisional_candle_count: number
  excluded_future_candle_count: number
  missing_interval_count: number
  structure_trend: "bullish" | "bearish" | "range" | "insufficient_data"
  structure_points: Array<{
    swing_kind: "high" | "low"
    label: string
    scope: "internal" | "external"
    price: number
    occurred_at: string
    confirmation: "confirmed" | "wick_only" | "provisional"
    detection_method: string
    evidence: LiquidityBarReference[]
  }>
  provisional_structure_points: Array<{
    swing_kind: "high" | "low"
    label: string
    scope: "internal" | "external"
    price: number
    occurred_at: string
    confirmation: "confirmed" | "wick_only" | "provisional"
    detection_method: string
    evidence: LiquidityBarReference[]
  }>
  liquidity_levels: Array<{
    level_id: string
    side: "buy_side" | "sell_side"
    price: number
    tolerance: number
    touches: number
    formed_at: string
    state: "untested" | "swept" | "invalidated"
    detection_method: string
    evidence: LiquidityBarReference[]
  }>
  sweeps: Array<{
    sweep_id: string
    level_id: string
    side: "buy_side" | "sell_side"
    level_price: number
    penetration_price: number
    occurred_at: string
    confirmation: "confirmed" | "wick_only" | "provisional"
    invalidated: boolean
    invalidation_reason: string | null
    detection_method: string
    evidence: LiquidityBarReference[]
  }>
  fair_value_gaps: Array<{
    gap_id: string
    direction: "bullish" | "bearish"
    lower_boundary: number
    upper_boundary: number
    gap_size: number
    formed_at: string
    state: "untouched" | "partially_filled" | "fully_filled"
    invalidated: boolean
    detection_method: string
    evidence: LiquidityBarReference[]
  }>
  structure_breaks: Array<{
    break_id: string
    kind: "bullish_bos" | "bearish_bos" | "bullish_choch" | "bearish_choch"
    reference_swing_bar_id: string
    reference_level: number
    break_price: number
    occurred_at: string
    confirmation: "confirmed" | "wick_only" | "provisional"
    invalidation_level: number
    invalidated: boolean
    detection_method: string
    evidence: LiquidityBarReference[]
  }>
  order_block_candidates: Array<{
    candidate_id: string
    direction: "bullish" | "bearish"
    source_bar_id: string
    lower_boundary: number
    upper_boundary: number
    formed_at: string
    associated_break_id: string
    state: "unmitigated" | "mitigated" | "invalidated"
    invalidation_level: number
    detection_method: string
    evidence: LiquidityBarReference[]
  }>
  warnings: string[]
  limitations: string[]
}

export type LiquidityIntelligenceReport = {
  calculation_mode: "deterministic_price_action"
  method_version: "liquidity-intelligence/v1"
  evaluated_at: string
  analyses: LiquidityTimeframeAnalysis[]
  alignment: {
    state: "aligned_bullish" | "aligned_bearish" | "mixed" | "insufficient_data"
    timeframes: string[]
    bullish_timeframes: string[]
    bearish_timeframes: string[]
    range_timeframes: string[]
    missing_timeframes: string[]
    method: string
    limitations: string[]
  }
  available_timeframes: string[]
  unavailable_timeframes: string[]
  institutional_order_flow_observed: false
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

export type ResearchAvailability = {
  status: "available" | "unavailable" | "unknown" | "stale" | "partial" | "not_requested"
  record_count: number
  reason_codes: string[]
  provider_id: string | null
  coverage_start: string | null
  coverage_end: string | null
  last_source_candle_at: string | null
  retrieved_at: string | null
  missing_interval_count: number
  provisional_count: number
  warning_codes: string[]
  error_category: string | null
}

export type ResearchInstrument = {
  expression: string
  status: "resolved" | "ambiguous" | "unresolved"
  identity_source: "catalogue" | "standard_reference" | "none"
  instrument_id: string | null
  canonical_instrument_id: string | null
  canonical_symbol: string | null
  category: "forex" | "commodity" | "crypto" | "stock" | "etf" | "future" | "index" | "bond" | null
  display_name: string | null
  base_currency: string | null
  quote_currency: string | null
  exchange_id: string | null
  exchange_resolution: "resolved" | "venue_neutral" | "legacy_unresolved" | null
  candidate_instrument_ids: string[]
  reason_codes: string[]
}

export type ResearchRequestContext = {
  schema_version: "research-request/v1"
  primary_instrument: ResearchInstrument
  comparison_instruments: ResearchInstrument[]
  timeframe: "1m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1d" | "1w" | null
  lookback: { value: number; unit: string } | null
  intents: string[]
  analysis_status: "research_preparation_only"
  unavailable_capabilities: string[]
  missing_information: string[]
  evidence: Record<
    "instrument_identity" | "current_quote" | "historical_ohlcv" | "derived_features" | "news" | "macro" | "positioning_order_flow",
    ResearchAvailability
  >
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

export type BacktestMetric = {
  status: "available" | "undefined" | "insufficient_sample"
  value: number | null
  unit: string
  reason: string | null
}

export type BacktestPerformanceMetrics = {
  total_trades: number
  winning_trades: number
  losing_trades: number
  breakeven_trades: number
  win_rate: BacktestMetric
  gross_profit: BacktestMetric
  gross_loss: BacktestMetric
  profit_factor: BacktestMetric
  average_win: BacktestMetric
  average_loss: BacktestMetric
  expectancy: BacktestMetric
  maximum_drawdown: BacktestMetric
  total_return: BacktestMetric
  average_holding_duration: BacktestMetric
  consecutive_wins: number
  consecutive_losses: number
  realized_risk_reward: BacktestMetric
  sharpe_ratio: BacktestMetric
  basis: "gross_returns"
  gross: BacktestReturnSeriesMetrics
  net: BacktestReturnSeriesMetrics | null
}

export type BacktestReturnSeriesMetrics = {
  basis: "gross_returns" | "net_returns"
  winning_trades: number
  losing_trades: number
  breakeven_trades: number
  win_rate: BacktestMetric
  profit: BacktestMetric
  loss: BacktestMetric
  profit_factor: BacktestMetric
  average_win: BacktestMetric
  average_loss: BacktestMetric
  expectancy: BacktestMetric
  maximum_drawdown: BacktestMetric
  total_return: BacktestMetric
  consecutive_wins: number
  consecutive_losses: number
  sharpe_ratio: BacktestMetric
}

export type BacktestTrade = {
  trade_id: string
  direction: "long" | "short"
  signal_at: string
  entered_at: string
  exited_at: string
  entry_price: number
  exit_price: number
  stop_loss: number
  take_profit: number
  exit_reason: string
  holding_bars: number
  holding_seconds: number
  gross_return_pct: number
  net_return_pct: number | null
  gross_monetary_pnl: number | null
  net_monetary_pnl: number | null
  position_size: number | null
  size_unit: "lots" | "contracts" | "units" | null
  costs_applied: boolean
}

export type BacktestReport = {
  status: "available" | "partial" | "insufficient_data" | "unavailable"
  strategy_id: "trend_confirmed_scenario_v1"
  symbol: string
  timeframe: string
  evaluated_at: string
  data_quality: {
    validated_bar_count: number
    completed_bar_count: number
    provisional_bar_count: number
    duplicate_timestamp_count: number
    missing_interval_count: number
    is_ordered: boolean
    coverage_start: string | null
    coverage_end: string | null
  }
  provenance: {
    series_id: string
    series_fingerprint: string
    source_id: string
    source_name: string
    retrieved_at: string
    timeframe: string
    venue: string | null
  } | null
  parameters: {
    strategy_id: "trend_confirmed_scenario_v1"
    development_fraction: number
    maximum_holding_bars: number
    walk_forward_window_bars: number | null
    walk_forward_step_bars: number | null
    collision_policy: "stop_first_conservative"
    entry_validity_bars: 1
    allow_overlapping_positions: false
  }
  cost_basis: "verified_net" | "gross_only"
  cost_assessment: {
    status: "unavailable" | "incomplete" | "verified" | "invalid"
    source_id: string | null
    verified_at: string | null
    spread_price: number | null
    slippage_price_per_side: number | null
    commission_per_size_round_trip: number | null
    missing_components: string[]
    assumptions: string[]
  }
  trades: BacktestTrade[]
  incomplete_trade_count: number
  metrics: BacktestPerformanceMetrics
  equity_curve: Array<{ timestamp: string; cumulative_return_pct: number; drawdown_pct: number }>
  net_equity_curve: Array<{ timestamp: string; cumulative_return_pct: number; drawdown_pct: number }>
  validation_windows: Array<{
    window_id: string
    kind: "development" | "out_of_sample" | "walk_forward"
    start_at: string
    end_at: string
    observation_count: number
    incomplete_trade_count: number
    metrics: BacktestPerformanceMetrics
    warnings: string[]
  }>
  warnings: string[]
  limitations: string[]
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

export type BacktestResponse = {
  report: BacktestReport
  quantitative_specialist: AgentAnalysis
  risk_specialist: AgentAnalysis
  master_synthesis: AnalyzeResponse["synthesis"]
}

export type HistoricalDatasetMetadata = {
  schema_version: 1
  dataset_version: 1
  dataset_id: string
  content_fingerprint: string
  provider_id: "twelve_data"
  provider_symbol: string
  canonical_symbol: string
  canonical_instrument_id: string | null
  requested_provider_exchange_id: string | null
  timeframe: string
  venue: string | null
  exchange_metadata_available: boolean
  provider_identity_verified: boolean
  adjustment_semantics: "provider_unspecified"
  requested_start_at: string
  requested_end_at: string
  coverage_start: string
  coverage_end: string
  retrieved_at: string
  validated_bar_count: number
  completed_bar_count: number
  provisional_bar_count: number
  observed_gap_count: number
  missing_interval_count: null
  provider_request_count: number
  completeness: "complete" | "partial"
  warning_codes: string[]
  created_at: string
}

export type HistoricalDatasetListResponse = {
  datasets: HistoricalDatasetMetadata[]
  corrupt_dataset_count: number
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

export type HistoricalDatasetIngestionResponse = {
  status: "available" | "partial" | "unavailable" | "not_configured" | "rate_limited" | "timeout" | "invalid_response" | "storage_error"
  dataset: HistoricalDatasetMetadata | null
  provider_request_count: number
  received_bar_count: number
  validated_bar_count: number
  reason_codes: string[]
  error_category: string | null
  production_authorization_available: false
  submission_allowed: false
  execution_authority: false
}

async function requestJson<T>(
  path: string,
  init: RequestInit,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...init.headers,
    },
    signal,
  })

  if (!response.ok) {
    throw new Error(`Nimbus API request failed with status ${response.status}`)
  }

  const contentType = response.headers.get("content-type") ?? ""
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new Error("Nimbus API returned an unexpected response format")
  }

  return response.json() as Promise<T>
}

const getJson = <T>(path: string, signal?: AbortSignal) =>
  requestJson<T>(path, { method: "GET" }, signal)

export const getHealth = (signal?: AbortSignal) =>
  getJson<HealthResponse>("/api/health", signal)

export const getProductionReadiness = (signal?: AbortSignal) =>
  getJson<ProductionReadinessReport>("/api/readiness", signal)

export const getAgents = (signal?: AbortSignal) =>
  getJson<AgentListResponse>("/api/agents", signal)

export const getLLMBudget = (signal?: AbortSignal) =>
  getJson<LLMBudgetStatus>("/api/llm/budget", signal)

export const getLLMUsage = (signal?: AbortSignal, limit = 100) =>
  getJson<LLMUsageRecord[]>(`/api/llm/usage?limit=${encodeURIComponent(limit)}`, signal)

export const getMarkets = (signal?: AbortSignal) =>
  getJson<MarketListResponse>("/api/markets", signal)

export const getInstruments = (filters: CatalogueFilters, signal?: AbortSignal) =>
  getJson<CatalogueResponse>(cataloguePath(filters), signal)

export const getInstrument = (instrumentId: string, signal?: AbortSignal) =>
  getJson<InstrumentResponse>(`/api/instruments/${encodeURIComponent(instrumentId)}`, signal)

export const analyzeDeskQuery = (query: string, signal?: AbortSignal) =>
  requestJson<AnalyzeResponse>(
    "/api/analyze",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    },
    signal,
  )

export const runHistoricalBacktest = (
  request: {
    symbol: string
    timeframe: string
    start_at?: string
    end_at?: string
    source_id?: string
    dataset_id?: string
    dataset_fingerprint?: string
  },
  signal?: AbortSignal,
) =>
  requestJson<BacktestResponse>(
    "/api/backtests",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    },
    signal,
  )

export const getHistoricalDatasets = (signal?: AbortSignal) =>
  getJson<HistoricalDatasetListResponse>("/api/historical-datasets", signal)

export const ingestHistoricalDataset = (
  request: {
    symbol: string
    timeframe: string
    start_at: string
    end_at: string
    max_requests?: number
    max_bars?: number
  },
  signal?: AbortSignal,
) => requestJson<HistoricalDatasetIngestionResponse>(
  "/api/historical-datasets/ingestions",
  {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  },
  signal,
)
