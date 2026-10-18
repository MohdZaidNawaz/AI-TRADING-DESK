# FRIDAY Core API

FastAPI service for the Nimbus Desk frontend and its deterministic agent engine. FRIDAY can ingest TradingView webhook events and manually requested Twelve Data market snapshots. It does not connect to brokers, MT5, order execution, automated trading, or AI providers.

## Setup

From the `backend` directory on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

For development and tests:

```powershell
python -m pip install -r requirements-dev.txt
```

## Run

```powershell
python -m uvicorn app.main:app --reload --no-access-log --host 127.0.0.1 --port 8000
```

The API is available at `http://127.0.0.1:8000`, with interactive documentation at `http://127.0.0.1:8000/docs`.

## Endpoints

- `GET /api/health`
- `GET /api/agents`
- `GET /api/markets`
- `POST /api/analyze`
- `POST /api/webhooks/tradingview/{webhook_token}`
- `POST /api/market-data/twelve-data/snapshot?symbol=XAU%2FUSD`

Example analysis request:

```json
{
  "symbol": "XAUUSD",
  "query": "Review technical momentum and risk"
}
```

The master orchestrator uses deterministic keyword routing. It returns a result for all nine specialists, marking agents that were not selected as `skipped`, followed by a mock synthesized response. The architecture under `app/agents` is intentionally isolated so model-backed agents and real data adapters can be introduced later.

## Multi-source ingestion architecture

The backend keeps external-source details outside FRIDAY Core:

```text
source connector -> normalized event -> ingestion service -> Evidence Store -> FRIDAY Core -> agents -> master synthesis
```

- `app/connectors/` contains source-specific adapters.
- `app/models/events.py` defines provider-neutral source metadata, data domains, asset classes, event types, and normalized event fields.
- `app/ingestion/` defines the `SourceConnector` contract and the provider-agnostic ingestion hand-off.
- `app/agents/` receives provider-neutral structured evidence; it does not parse provider payloads.

TradingView is a webhook market-data connector. Twelve Data is an API market-data connector using the same provider-neutral flow. Future market-data, economic-calendar, fundamental, forex, crypto, commodities, sentiment, or alternative-data integrations should implement `SourceConnector`, normalize their payload into `NormalizedDataEvent`, and send it through `IngestionService`. Financial-news and research connectors use `NewsResearchConnector` and `NormalizedNewsResearchEvent` instead.

## Phase 2 structured evidence architecture

FRIDAY converts validated normalized market and news events into a safe, immutable evidence projection before specialists consume it:

```text
NormalizedDataEvent / NormalizedNewsResearchEvent
    -> bounded process-local EvidenceStore
    -> immutable EvidenceSnapshot
    -> provider-neutral EvidenceRouter
    -> specialist-specific AgentEvidence
    -> AnalysisContext
```

`app/evidence/` contains the provider-neutral evidence projections, thread-safe bounded store, generic symbol matching, and nine-specialist routing policy. The store is process-local and non-persistent, so it resets when the API process restarts. It retains only bounded market/news evidence and creates deterministic snapshots without exposing mutable internal collections.

Evidence projections retain safe normalized fields and provenance only. They deliberately exclude provider `raw_payload` and provider-only attributes from market evidence, and exclude full article bodies from news evidence. The original normalized event remains unchanged for compatible API responses and transport behavior.

All nine specialists receive only their declared evidence subset. News is shared evidence, not a tenth specialist seat. Pilot Execution can receive read-only context but remains disabled: this architecture has no execution authority, broker integration, MT5 connection, credentials, order placement, or automated trading.

## Phase 3 trade proposal and Sentinel risk gate

Phase 3 adds internal, provider-neutral contracts for a non-executable `TradeProposal` and a `SentinelDecision`. A proposal references only safe evidence provenance, specialist-result digests, and optional observed normalized market facts. It does not contain raw provider payloads, article bodies, credentials, or executable instructions.

```text
immutable EvidenceSnapshot
    -> nine-specialist analysis
    -> F.R.I.D.A.Y. non-executable draft proposal
    -> Sentinel risk decision
    -> APPROVED / REJECTED / INSUFFICIENT_DATA
    -> future Pilot hand-off boundary (disabled)
```

The existing `/api/analyze` contract is unchanged. Phase 3 has no proposal or execution endpoint. FRIDAY's current deterministic agents do not produce structured direction, entry, stop, target, sizing, portfolio, or risk-limit data, so ordinary runs remain non-executable and Sentinel returns `INSUFFICIENT_DATA` rather than inventing those values.

An approval can exist only when explicit proposal parameters, fresh market evidence under an explicitly supplied policy, explicit portfolio/sizing context, structured specialist agreement, and any required news-risk assessment are all available. Invalid trade mathematics or configured risk-limit violations are `REJECTED`; absent or stale information is `INSUFFICIENT_DATA`.

Phase 3 originally used a lock-protected process-local registry. The current
compatibility facade delegates Sentinel approval ownership to the structurally
verified shared SQLite store described below. Each approval remains bound to
the proposal, analysis, evidence, policy, assessment, account, evaluation time,
and expiry; verification is read-only and consumption is single-use and
restart-safe. This durable ownership does not enable Pilot or broker execution.

Pilot remains disabled in Phase 3. It verifies a matching registry-issued Sentinel approval and then returns `DISABLED_NO_EXECUTION`; it has no adapter, no execution API route, no MT5 import, no broker connection, no credentials, and no order placement capability.

## Phase 4 structured specialist intelligence

Phase 4 preserves the existing `/api/analyze` response while adding an internal,
immutable specialist-intelligence layer between the evidence snapshot and the
Phase 3 proposal workflow:

```text
immutable EvidenceSnapshot
    -> specialist-specific AgentEvidence
    -> public AgentAnalysis + internal SpecialistAnalysis
    -> F.R.I.D.A.Y. structured specialist synthesis
    -> non-executable TradeProposal traceability
    -> Sentinel risk gate
```

`SpecialistAnalysis` is provider-neutral and contains only safe evidence
provenance references, deterministic fingerprints, directly observed facts,
explicitly separate interpretations, limitations, invalidating conditions, and
an analytical stance. It never contains provider payloads, article bodies,
trade direction, entry, stop loss, target, sizing, credentials, broker details,
or execution instructions.

`BULLISH`, `BEARISH`, `NEUTRAL`, `CONFLICTED`, and `INSUFFICIENT_DATA` are
analytical stances, not trade directions. They are not automatically converted
to `LONG` or `SHORT`, and they do not populate `RiskContext.specialist_signals`.
Sentinel's separate explicit policy, portfolio, proposal, and signal
requirements remain mandatory for every approval.

Current deterministic specialists retain only observations justified by their
routed evidence. In particular, Chartist records supplied OHLC/timeframe/volume
facts without inventing technical signals; Oracle records finite numeric values
without producing probabilities; Capitol and Athena remain `INSUFFICIENT_DATA`
without positioning or fundamental datasets; Ledger records only current-run
lifecycle/provenance; and Pilot remains `DISABLED_NO_EXECUTION`.

The artifacts are internal during Phase 4. No API route, public schema,
provider connector, polling process, broker integration, MT5 adapter, or
execution capability is added.

## Phase 5A historical market-series foundation

Phase 5A adds a separate provider-neutral path for validated historical OHLCV
bars. It coexists with point-in-time market events and news evidence instead of
changing either contract:

```text
NormalizedMarketBar
    -> bounded process-local MarketSeriesStore
    -> immutable MarketSeriesEvidence in EvidenceSnapshot
    -> EvidenceRouter
    -> Chartist Technician and Oracle Quant only
```

`NormalizedMarketBar` requires canonical symbol and timeframe values,
timezone-aware UTC-normalized timestamps, finite OHLC values with valid high
and low bounds, exact positive bar timing, explicit completed/provisional state,
and explicit volume semantics (`unknown`, `real`, `exchange`, `tick`, or
`unavailable`). A semantic bar ID is derived from source, symbol, venue,
timeframe, and opening timestamp; revisions keep that identity and are resolved
deterministically by explicit revision number or receipt time.

`MarketSeriesStore` is thread-safe, bounded by both series count and bars per
series, and process-local. It orders bars chronologically, deduplicates semantic
bar identities, records continuity gaps without filling or inventing bars, and
returns immutable snapshots with stable fingerprints and source provenance.
The store is cleared whenever the backend process restarts.

Historical series are routed only to Chartist Technician and Oracle Quant.
Chartist may record direct bar counts, timeframe, timestamps, OHLCV values,
volume kind, and continuity metadata. Oracle may record the same finite numeric
observations. Phase 5A does not calculate indicators, returns, volatility,
probabilities, trend labels, levels, breakouts, or trade direction. Series
fingerprints and latest completed-bar references participate in proposal
traceability, but they do not weaken or satisfy any existing Sentinel approval
requirement.

This phase provides an internal ingestion service only. It adds no API route,
provider client, live request, polling job, database, cache, frontend change,
MT5 or broker adapter, order placement, or execution authority. Future provider
adapters must normalize their own history payloads into `NormalizedMarketBar`
before calling this boundary. Twelve Data historical-series integration is
intentionally deferred until its official capability and account entitlement
are verified; Phase 5A does not call or alter the Twelve Data connector.

## Phase 5B-1 deterministic derived market features

Phase 5B-1 adds a provider-neutral, side-effect-free calculation layer over an
immutable Phase 5A `MarketSeriesEvidence` snapshot. Derived values are calculated
once per analysis run, are not persisted in `EvidenceStore` or
`MarketSeriesStore`, and remain explicitly separate from raw observations:

```text
immutable EvidenceSnapshot.market_series
    -> DerivedFeatureService (one calculation snapshot per analysis run)
    -> DerivedFeatureRouter
    -> AnalysisContext.derived_features
    -> Chartist / Oracle structured derived observations
```

The approved deterministic feature set is: `BAR_RANGE = high - low`;
`AVERAGE_RANGE(N) = mean(high - low)` over exactly N bars;
`SIMPLE_RETURN = current_close / previous_close - 1`;
`LOG_RETURN = ln(current_close / previous_close)`; `SMA(N) = mean(close)` over
exactly N bars; `ROC(N) = current_close / close_N_periods_ago - 1`; and
`ROLLING_STD(N)`, the population standard deviation of exactly N contiguous
simple returns (`ddof=0`, requiring N+1 closes). Parameterized calculations
require an explicit positive integer `window`; FRIDAY does not silently choose
a research horizon. The default orchestration requests only the
non-parameterized range, simple-return, and log-return calculations.

Every feature records a stable ID, method ID and version, canonical parameters,
input series ID and fingerprint, exact first/last input bar IDs, required and
actual bar counts, timestamp, unit, quality flags, status, and limitations.
Only completed bars are eligible. Provisional bars are excluded and reported;
gaps inside a required window prevent calculation; gaps before the selected
latest window remain explicit quality metadata. Insufficient history, undefined
ratio/log domains, and invalid or non-finite input never produce a numeric value.

Chartist receives range, average-range, SMA, and ROC features. Oracle receives
simple return, log return, rolling return standard deviation, and ROC. The other
seven specialists receive no derived features. Specialists only describe routed
calculations and their lineage; they do not recompute values or convert them into
trade direction, signals, confidence, position sizing, or execution authority.
Master synthesis retains derived traceability separately from raw evidence.
Sentinel requirements are unchanged, and Pilot remains disabled.

This phase adds no API route, external request, provider integration, polling,
database, cache, frontend change, broker connection, MT5 adapter, order
placement, or automated execution.

## Phase 5B-2 structured economic-event evidence

Phase 5B-2 adds an internal provider-neutral boundary for discrete economic
calendar and release events. No live economic provider is connected:

```text
future provider connector
    -> immutable NormalizedEconomicEvent
    -> safe EconomicEventEvidence
    -> bounded EconomicEventStore
    -> immutable EvidenceSnapshot.economic_events
    -> EvidenceRouter
    -> Atlas / Scout / Sentinel / Ledger structured artifacts
```

Economic values use finite decimals with explicit units and scales; FRIDAY does
not silently convert them. Each immutable lifecycle version is one of
`scheduled`, `released`, `revised`, `delayed`, `cancelled`, or `unknown`.
Versions from the same provider occurrence retain a stable occurrence ID, a
positive release version, deterministic content fingerprint, and deterministic
event ID. Receipt-time noise is excluded from content identity. Exact
duplicates collapse, conflicting content for the same provider occurrence and
version is rejected, and provider occurrences are not reconciled automatically.

The process-local `EconomicEventStore` is thread-safe, bounded, deterministically
ordered, and non-persistent. It preserves retained lifecycle history and can
select the current version for each provider occurrence. Economic evidence is
kept separate from point-in-time market events, news, and historical market
series. The orchestrator captures one immutable snapshot for an analysis run;
specialists do not read mutable store state independently.

Atlas receives current scheduled, released, and revised macro evidence. Scout
also receives delayed and cancelled catalyst lifecycle context. Sentinel sees
only event identity, timing, status, expected impact, and provenance context;
economic values are withheld from its specialist input. Ledger retains the full
bounded lifecycle and revision history. The other five specialists receive no
economic-event evidence.

Forecast, previous, revised-previous, and actual values remain separate typed
fields. This phase performs no economic surprise calculation, creates no
automatic stance or trade direction, derives no entry, stop, target, or sizing,
and does not satisfy Sentinel's market-evidence requirements. It creates no
Sentinel approval, and Pilot remains disabled. There is no new public API route,
frontend change, credential, polling process, persistence layer, broker/MT5
adapter, order placement, or execution authority.

## Phase 5B-3 provider-neutral macro-series evidence

Phase 5B-3 adds a distinct internal path for persistent observed macroeconomic
series. It remains separate from point-in-time market events, OHLCV
`MarketSeriesEvidence`, and discrete `EconomicEventEvidence` lifecycle records:

```text
future macro connector
    -> immutable NormalizedMacroObservation
    -> bounded process-local MacroSeriesStore
    -> immutable MacroSeriesEvidence in EvidenceSnapshot.macro_series
    -> EvidenceRouter
    -> Atlas Macro structured observations
    -> F.R.I.D.A.Y. internal specialist synthesis
```

Macro observations use the existing finite-`Decimal` `EconomicValue` contract
with explicit unit and scale. Their source-scoped series identity includes the
source series identifier, FRIDAY indicator identifier, geography, currency,
maturity, frequency, seasonal-adjustment status, and value unit/scale. This
keeps, for example, US 2Y/10Y nominal yields, explicitly maturitied real yields,
and a broad dollar index distinct. Providers are never reconciled automatically.

Each observation preserves a structured reference period separately from
`observed_at`, `effective_at`, `published_at`, `vintage_at`, and `received_at`.
This supports daily yields, monthly CPI, quarterly GDP, and policy-rate levels
without forcing them into fixed-second market-bar semantics. `received_at` is
provenance only; there are no freshness thresholds or stale-data policy rules.

The process-local store is thread-safe and bounded by series, reference-period
occurrences, and retained vintages. It preserves retained revision history,
selects the highest retained version as current, collapses exact duplicates, and
rejects conflicting content for an identical source/series/reference-period/
version identity. Its separate conflict-fingerprint registry is also bounded;
post-eviction conflict detection therefore has a documented deterministic
retention horizon and retains no provider payloads or full historical records.

Only Atlas receives macro-series values in Phase 5B-3. Ledger receives only
provenance references. The remaining specialists, including Sentinel and Pilot,
receive no macro-series evidence. Macro evidence cannot satisfy Sentinel's
market-evidence requirements, populate risk signals, create approvals, or enable
execution. Atlas remains `INSUFFICIENT_DATA`; this phase adds no macro-derived
calculations, macro stance policy, signals, trade decisions, providers,
persistence, polling, API route, credentials, frontend changes, broker/MT5
adapter, or order placement.

## Phase 5B-4 provider-neutral positioning evidence

Phase 5B-4 adds a distinct internal path for reported positioning,
participation, holdings, open-interest, and capital-flow observations. It is
separate from price events, OHLCV bars, derived features, economic releases,
and macroeconomic series:

```text
future positioning connector
    -> immutable NormalizedPositioningObservation
    -> bounded process-local PositioningStore
    -> immutable EvidenceSnapshot.positioning_series
    -> EvidenceRouter
    -> Capitol Smart Money structured observations
    -> F.R.I.D.A.Y. internal specialist synthesis
```

Each observation has a source-scoped series identity, instrument identifier,
optional symbol and venue, participant category, measure, frequency, explicit
finite value unit/scale, reported reference period, and revision/vintage
metadata. A connector must map its own labels into these provider-neutral
contracts before ingestion; no instrument aliases or provider payloads are
accepted by FRIDAY's evidence or agent layers.

The process-local store is thread-safe and bounded by series, reference-period
occurrences, retained vintages, and a separate bounded conflict-fingerprint
registry. Exact duplicates collapse. Conflicting content for the same source,
series, reference period, and version is rejected, including after full-record
eviction while the documented deterministic metadata-retention horizon remains.
There is no provider reconciliation, persistence, cache, polling job, or live
positioning connector in this phase.

Only Capitol receives typed positioning values, with a deterministic per-agent
observation limit that protects specialist-artifact bounds. Ledger receives
provenance references only. Atlas, Scout, Athena, Chartist, Oracle, Sentinel,
and Pilot receive no positioning evidence. Capitol retains factual observations
but remains `INSUFFICIENT_DATA`: FRIDAY does not infer institutional intent or
net exposure, calculate signals/probabilities, create a stance or trade
direction, or create trade parameters from positioning data.

Positioning provenance participates in internal proposal traceability only. It
is not market evidence, does not populate `RiskContext.specialist_signals`, and
does not alter Sentinel policy, approval criteria, or Pilot's disabled state.
The public `/api/analyze` schema is unchanged. This phase adds no API route,
frontend work, credentials, broker/MT5 adapter, order placement, or execution
authority.

## Phase 5B-5 provider-neutral fundamental financial-statement evidence

Phase 5B-5 adds a distinct internal path for reported issuer accounting facts.
It is separate from price events, OHLCV bars, derived features, economic
releases, macro series, positioning, estimates, valuation calculations,
comparables, corporate actions, and company profiles:

```text
future fundamental connector
    -> immutable NormalizedFundamentalObservation
    -> bounded process-local FundamentalStore
    -> immutable EvidenceSnapshot.fundamental_series
    -> EvidenceRouter
    -> Athena Analyst structured observations
    -> F.R.I.D.A.Y. internal specialist synthesis
```

Each observation has source-scoped issuer and series identity, statement kind,
normalized metric identifier, reporting scope/frequency, explicit fiscal or
point-in-time reference period, finite typed value, currency/unit/scale, and
revision/vintage metadata. Connector-specific issuer and metric labels must be
mapped before ingestion. FRIDAY does not reconcile issuer identities across
providers or infer heuristic aliases. Raw provider payloads are excluded from
the contracts, evidence snapshots, specialists, and proposal traceability.

The process-local store is thread-safe and bounded by series, reporting-period
occurrences, retained vintages, and a separate bounded conflict-fingerprint
registry. Exact duplicates collapse. Conflicting content for the same source,
series, period, and version is rejected, including after full-record eviction
while the deterministic metadata-retention horizon remains. No provider,
polling job, API route, database, cache, or credential is added.

Only Athena receives typed financial-statement values, with a deterministic
per-agent observation limit. Ledger retains provenance references only. Atlas,
Capitol, Scout, Chartist, Oracle, Sentinel, and Pilot receive no fundamental
values. Athena remains `INSUFFICIENT_DATA`: this phase records reported facts
without calculating valuation or financial ratios, estimates, forecasts,
probabilities, directions, entries, stops, targets, or sizing.

Fundamental provenance participates in internal proposal traceability only. It
is not market evidence, cannot satisfy Sentinel freshness, portfolio, sizing,
or specialist-agreement requirements, and does not populate risk signals or
create approval authority. Pilot remains `DISABLED_NO_EXECUTION`. The public
`/api/analyze` schema is unchanged. This phase adds no frontend work, MT5 or
broker adapter, order placement, or execution authority.

## Phase 5B-6 provider-neutral analyst-estimate evidence

Phase 5B-6 adds a distinct internal path for source-reported analyst estimates.
It is separate from reported financial statements, price events, OHLCV bars,
derived features, economic releases, macro series, positioning, valuations,
comparables, recommendations, ratings, and price targets:

```text
future estimate connector
    -> immutable NormalizedEstimateObservation
    -> bounded process-local EstimateStore
    -> immutable EvidenceSnapshot.estimate_series
    -> EvidenceRouter
    -> Athena Analyst structured estimate observations
    -> F.R.I.D.A.Y. internal specialist synthesis
```

Each observation retains a source-scoped issuer and series identity, an explicit
target fiscal or point-in-time period, permitted source-reported statistic
(`consensus_mean`, `median`, `high`, `low`, or `analyst_count`), finite typed
value, `as_of` time, optional publication/vintage times, and immutable revision
metadata. The target period is not the estimate vintage. Connector-specific
issuer and metric labels must be mapped before ingestion; FRIDAY does not
reconcile issuers across providers or infer aliases. Raw provider payloads are
excluded from contracts, snapshots, specialist artifacts, and proposal traces.

The process-local store is thread-safe and bounded by series, target-period
occurrences, retained vintages, and a separate bounded conflict-fingerprint
registry. Exact duplicates collapse. Conflicting content for the same source,
series, target period, and version is rejected while its deterministic
metadata-retention horizon remains. No provider, polling job, API route,
database, cache, or credential is added.

Only Athena receives typed estimate values, with a deterministic per-agent
observation limit. Ledger retains provenance references only. Atlas, Capitol,
Scout, Chartist, Oracle, Sentinel, and Pilot receive no estimate values. Athena
remains `INSUFFICIENT_DATA`: source-reported expectations are factual context
only and do not create a FRIDAY forecast, valuation, ratio, probability,
direction, entry, stop, target, or sizing instruction.

Estimate provenance participates in internal proposal traceability only. It is
not market evidence, cannot satisfy Sentinel freshness, portfolio, sizing,
risk-context, or specialist-agreement requirements, and does not populate risk
signals or create approval authority. Pilot remains `DISABLED_NO_EXECUTION`.
The public `/api/analyze` schema is unchanged. This phase adds no frontend work,
MT5 or broker adapter, order placement, or execution authority.

## Phase 5B-7 provider-neutral corporate-action evidence

Phase 5B-7 adds a distinct internal path for source-reported issuer actions:
cash dividends, stock splits, and reverse stock splits. It is separate from
prices, OHLCV bars, derived features, financial statements, analyst estimates,
valuation calculations, and total-return or historical-price adjustment logic:

```text
future corporate-action connector
    -> immutable NormalizedCorporateAction
    -> bounded process-local CorporateActionStore
    -> immutable EvidenceSnapshot.corporate_actions
    -> EvidenceRouter
    -> specialist artifacts
```

Each action has a source-scoped issuer/instrument identity, provider occurrence
ID, lifecycle, explicit published/effective times, optional discrete calendar
dates, immutable version/revision linkage, and typed terms. Cash dividends
require a finite currency amount and explicit currency. Share adjustments
require positive whole-number numerator/denominator terms. Raw provider payloads
are excluded from contracts, snapshots, specialist artifacts, and proposal
traces. FRIDAY does not reconcile issuers across providers or infer aliases.

The process-local store is thread-safe and bounded by source-scoped
instruments, action occurrences, versions, and a separate bounded
conflict-fingerprint registry. Exact duplicates are idempotent. Conflicting
content for the same source, instrument, occurrence, and version is rejected
while its deterministic metadata-retention horizon remains. No provider, API
route, polling job, database, cache, or credential is introduced in this phase.

Athena Analyst and Scout Recon receive typed corporate-action facts. Chartist
Technician receives only split and reverse-split facts to contextualize a
possible series discontinuity. Ledger The Book receives provenance references
only. Atlas, Capitol, Oracle, Sentinel, and Pilot receive no corporate-action
values. These facts do not create price adjustments, dividend-yield or
total-return calculations, valuation, forecasts, probabilities, market stance,
trade direction, entries, stops, targets, or sizing.

Corporate-action provenance participates in internal proposal traceability only.
It is not market evidence, cannot satisfy Sentinel freshness, portfolio, sizing,
risk-context, or specialist-agreement requirements, and does not populate risk
signals or create approval authority. Pilot remains `DISABLED_NO_EXECUTION`.
The public `/api/analyze` schema is unchanged. This phase adds no frontend work,
MT5 or broker adapter, order placement, or execution authority.

## Phase 5B-8 provider-neutral issuer and listed-instrument reference evidence

Phase 5B-8 adds a distinct internal path for source-reported issuer and listed
instrument reference profiles. It retains identity and classification context,
not prices, statements, estimates, actions, valuation, peer analysis, ticker
aliasing, or entity resolution:

```text
future issuer-reference connector
    -> immutable NormalizedIssuerInstrumentReference
    -> bounded process-local IssuerReferenceStore
    -> immutable EvidenceSnapshot.issuer_references
    -> EvidenceRouter
    -> Athena Analyst / Scout Recon factual specialist artifacts
```

Each profile is scoped to its reporting source and contains source issuer and
instrument IDs, optional source-reported symbol and venue, names, asset class,
instrument kind, source-reported status, explicit classification taxonomy,
optional country/domicile/currency, `as_of` and optional publication time, and
immutable version/revision metadata. Timestamps are timezone-aware and
normalized to UTC. Local `received_at` is retained for provenance but excluded
from semantic identity and fingerprints. Raw provider payloads, credentials,
URLs with credentials, floats, recommendations, confidence, direction, and
trade parameters are excluded.

The process-local store is thread-safe and bounded by 100 source-scoped
instruments, five profile occurrences per instrument, five retained versions
per occurrence, and an independently bounded conflict-fingerprint registry.
Exact duplicates are idempotent. Conflicting content for the same source,
instrument, profile occurrence, and version is rejected while deterministic
metadata-retention permits. No provider, API route, polling job, database,
cache, or credential is introduced in this phase.

Athena Analyst and Scout Recon receive typed factual reference observations.
Ledger The Book receives provenance references only. Atlas, Capitol, Chartist,
Oracle, Sentinel, and Pilot receive no issuer-reference values. The evidence is
traceability-only for proposal workflows: it is not market evidence, cannot
satisfy Sentinel requirements, does not populate risk signals, and creates no
approval authority. Pilot remains `DISABLED_NO_EXECUTION`.

Future work may separately assess profile providers, taxonomy mapping,
entity-resolution policy, ticker changes, valuation, comparables, and issuer
lifecycle semantics. This foundation intentionally adds none of them. The
public `/api/analyze` schema and frontend remain unchanged.

## Phase 5B-9 provider-neutral portfolio and account-risk context evidence

Phase 5B-9 adds a distinct internal path for source-reported account state,
positions, and explicit portfolio/account limits. It is evidence only, not a
broker integration or an account-management adapter:

```text
future portfolio-risk connector
    -> immutable NormalizedPortfolioRiskSnapshot
    -> bounded process-local PortfolioRiskStore
    -> immutable EvidenceSnapshot.portfolio_risk_snapshots
    -> EvidenceRouter
    -> Sentinel Risk Officer factual specialist artifact
```

Each snapshot is source-scoped to a source account identity and may preserve
explicitly reported balance, equity, funds, margin, P&L, position side,
quantity, source-reported prices, and source-reported limits. Monetary values
use typed finite decimal `EconomicValue` values; no binary floating-point
financial data is used. Source account identities are never reconciled across
providers. Raw provider payloads, credentials, secrets, authentication headers,
and credential-bearing URLs are excluded.

The store is thread-safe and bounded by 100 accounts, 500 snapshot occurrences
per account, five retained versions per occurrence, and an independently
bounded conflict-fingerprint registry. Exact duplicates are idempotent, while
conflicting content for the same source/account/occurrence/version is rejected
within its deterministic metadata-retention horizon. `received_at` is retained
for provenance but excluded from semantic identity and fingerprints.

Sentinel Risk Officer is the sole typed consumer. Ledger The Book receives
provenance references only; Atlas, Capitol, Scout, Athena, Chartist, Oracle,
and Pilot receive no portfolio-risk values. Reported portfolio evidence can
satisfy only Sentinel's existing `portfolio_context` availability requirement.
It cannot replace market evidence, freshness, sizing policy, specialist
agreement, or event/news-risk assessment, and it cannot independently approve
a proposal.

This phase does not calculate exposure, leverage, drawdown, margin utilization,
portfolio heat, correlation, concentration, sizing, liquidation risk, or stop
distance. It produces no trade direction, trade parameters, approval decision,
or execution instruction. Pilot remains `DISABLED_NO_EXECUTION`; no MT5,
broker SDK, provider, API route, credential, frontend, or execution capability
is introduced.

## Phase 5B-10 provider-neutral portfolio-risk metrics and exposure evidence

Phase 5B-10 is a separate deterministic layer over Phase 5B-9 source-reported
portfolio snapshots. Phase 5B-9 records what a source reports; Phase 5B-10
records only what FRIDAY can calculate exactly from those typed facts:

```text
NormalizedPortfolioRiskSnapshot
    -> PortfolioRiskStore
    -> deterministic PortfolioRiskMetricsService (portfolio-risk-v1)
    -> bounded PortfolioRiskMetricsStore
    -> EvidenceSnapshot.portfolio_risk_metric_snapshots
    -> Sentinel Risk Officer factual artifact
```

The only supported metric families are equity utilization (`equity / balance`),
margin utilization (`margin_used / (margin_used + margin_available)`), account
drawdown against an explicitly source-reported reference equity, daily-loss
utilization against an explicitly source-reported daily-loss limit, absolute
position exposure (`abs(quantity * reference_price)`), and position
concentration against a complete set of available source-reported position
exposures. All arithmetic uses finite `Decimal` values. Each metric retains its
calculation version, source snapshot identity/fingerprint, timestamp, status,
and explicit typed input references.

Missing, incompatible, non-positive-denominator, or incomplete inputs produce
an `insufficient_inputs` metric state. FRIDAY never estimates a missing value,
substitutes balance for a reference equity, infers a prop-firm rule, derives
leverage, or constructs a partial portfolio-total denominator. Receipt time is
provenance only and excluded from semantic metric identities and fingerprints.

The metric store is process-local, thread-safe, bounded, deterministic, and
retains a bounded post-eviction conflict-fingerprint registry. Exact duplicate
calculations are idempotent; semantically conflicting calculations for the
same source/account/source-snapshot occurrence/version/calculation-version are
rejected while retained conflict metadata permits.

Sentinel Risk Officer is the sole typed metric consumer. The metrics are factual
risk context only: they add no thresholds, automatic approval/rejection,
direction, recommendation, position sizing, leverage recommendation, entry,
stop, target, execution authority, provider integration, API route, or frontend
change. Ledger receives no unrestricted metric values, Pilot remains
`DISABLED_NO_EXECUTION`, and the public `/api/analyze` contract is unchanged.

## Phase 6A — Master Decision Thesis + Explicit Proposal Assembly

Phase 6A-1 inserts one immutable `MasterTradeThesis` into FRIDAY's internal
analysis flow:

```text
nine SpecialistAnalysis artifacts
    -> MasterSpecialistSynthesis
    -> MasterTradeThesis
    -> non-executable TradeProposal draft
    -> SentinelRiskGate
    -> disabled Pilot boundary
```

The thesis is a provider-neutral analytical artifact built only from the
structured specialist artifacts, Master synthesis, and safe evidence references
already captured for the immutable analysis run. It has a deterministic ID and
fingerprint, preserves artifact and evidence lineage, records factual coverage
metadata, and represents only analytical bias: bullish, bearish, neutral,
conflicted, or insufficient data.

Analytical bias is deliberately distinct from trade direction. A bullish thesis
does not create a `LONG` proposal; a bearish thesis does not create a `SHORT`
proposal. The thesis cannot determine an entry, stop loss, target, risk/reward,
position size, leverage, probability, approval, or execution instruction. It
does not alter Sentinel policy or satisfy market freshness, portfolio context,
sizing, specialist-agreement, or event/news-risk requirements.

The existing proposal flow may retain only a compact thesis ID/fingerprint for
traceability. Sentinel remains the sole approval gate and Pilot remains
`DISABLED_NO_EXECUTION`. No provider, API route, frontend, broker, MT5,
credential, or execution change is included.

### Explicit proposal assembly

`ExplicitTradeProposalInput` is an internal, provider-neutral boundary for a
human or future external capability to supply a direction, reference price,
stop loss, targets, risk/reward, sizing intent, requested risk fraction, and
ordinary-text rationale. It must reference the exact immutable
`MasterTradeThesis` ID and fingerprint produced for the captured analysis.

`ProposalAssemblyService` validates the supplied thesis, analysis ID, canonical
symbol, and evidence fingerprint against one immutable `EvidenceSnapshot`.
It copies only the supplied parameter values into an immutable pending
`TradeProposal`; it never derives a direction or any parameter from specialist
stances, Master synthesis, thesis bias, prices, indicators, or evidence.

The internal explicit workflow is deliberately separate from `/api/analyze`.
It runs only when explicitly invoked with `ExplicitTradeProposalInput`, passes
the assembled final proposal to Sentinel, and verifies that proposal provenance
exactly matches the snapshot received by Sentinel. The thesis remains
analytical, non-authorizing, and non-executable; it cannot satisfy Sentinel
market freshness, portfolio, sizing, agreement, or news/event-risk rules.
Only Sentinel can register an approval. Pilot continues to validate a
registry-backed, unexpired, one-time approval and always returns
`DISABLED_NO_EXECUTION` without invoking an execution adapter.

## News and research foundation

The backend includes a provider-neutral staging layer for future financial-news and research sources:

```text
future provider connector -> NormalizedNewsResearchEvent -> NewsResearchIngestionService -> EvidenceStore
```

`NormalizedNewsResearchEvent` records stable source metadata, provider, title, HTTP(S) URL, timezone-aware timestamp, optional symbols and topics, and either a short summary or content value. `NewsResearchConnector` defines the future provider-adapter boundary, ensuring provider-specific parsing remains outside FRIDAY Core. `NewsResearchIngestionService` keeps a bounded, process-local collection of validated events.

This foundation does not register an API route, scrape or poll websites, call an external provider, or require credentials. `NewsResearchIngestionService` can hand validated provider-neutral events to the same process-local Evidence Store used by FRIDAY Core; it does not alter Alpha Vantage retrieval behavior or create background polling. A future provider integration must normalize its payload at the connector boundary before ingestion.

## Twelve Data market snapshots

Set `TWELVE_DATA_API_KEY` in the backend process environment before starting the API. The process environment takes precedence; for local development only, the backend also reads this single setting from `backend/.env` when the process variable is absent. Keep the value in your operating-system or deployment secret manager; never commit it or place it in a URL, request body, test, or documentation.

For local PowerShell use, set the environment variable in the same terminal that starts Uvicorn:

```powershell
$env:TWELVE_DATA_API_KEY = Read-Host "Twelve Data API key"
python -m uvicorn app.main:app --reload --no-access-log --host 127.0.0.1 --port 8000
```

Request the initial supported test instrument, using Twelve Data's `XAU/USD` symbol format:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/market-data/twelve-data/snapshot?symbol=XAU%2FUSD"
```

The backend calls Twelve Data's quote endpoint, authenticates in the outbound request header, normalizes the response into `NormalizedDataEvent`, and sends it through the shared `IngestionService` into FRIDAY Core. The API response contains the normalized market event, never the API key. Provider failures return a generic error without provider credentials or response details. This manual route performs one upstream request and is intended for development and controlled testing; it is not a live stream or trading endpoint.

## TradingView webhooks

`POST /api/webhooks/tradingview/{webhook_token}` receives TradingView alert JSON, validates it, normalizes it into an internal market event, and acknowledges it immediately with HTTP 200. FRIDAY Core processes the normalized event in a background task through the existing deterministic analysis flow; the endpoint never executes trades or connects to brokers, MT5, or market-data providers.

### Authentication

Generate a long random token locally and set it as `TRADINGVIEW_WEBHOOK_SECRET` before starting the API. Configure the same token as the final path segment of the TradingView webhook URL. The API compares the URL token with a constant-time check, never includes it in a response, and never reads credentials from the JSON payload.

The API fails closed with HTTP 503 when this environment variable is unset or blank, and returns HTTP 401 for an incorrect URL token. Store a unique, high-entropy value in your operating-system or deployment secret manager; never commit it, add it to source files, or use a real secret in tests.

Treat the full webhook URL as a secret: use HTTPS, do not share it publicly, and rotate the token immediately if it is exposed. The application does not log the token. Configure any reverse proxy or observability system to redact `/api/webhooks/tradingview/*`; the documented Uvicorn command disables access logs so it does not record request paths.

### Alert message template

The connector supports a resolved `symbol` or `ticker`, `exchange`, `timeframe`/`interval`, `event`/`action`, `alert_name`, `message`, OHLCV fields, `price`, and an ISO-8601 or Unix `time`/`timestamp`. All price and volume fields are optional except the resolved symbol/ticker.

Configure TradingView with a JSON message such as the following. TradingView resolves the placeholders before sending the alert:

```json
{
  "symbol": "{{exchange}}:{{ticker}}",
  "timeframe": "{{interval}}",
  "event": "price_alert",
  "alert_name": "FRIDAY market observation",
  "message": "Optional context defined in the TradingView alert.",
  "price": "{{close}}",
  "time": "{{time}}",
  "open": "{{open}}",
  "high": "{{high}}",
  "low": "{{low}}",
  "close": "{{close}}",
  "volume": "{{volume}}",
  "indicators": {
    "custom_plot_one": "{{plot_0}}",
    "custom_plot_two": "{{plot_1}}"
  }
}
```

Indicator and plot values are optional. Send any object of unique string keys with finite numeric values in `indicators`, `plots`, or `indicator_values`; all are normalized into the provider-neutral `indicator_values` map. The connector does not reserve or interpret indicator names, so Pine alerts can use any plots or calculations without requiring backend changes.

The normalizer separates an `EXCHANGE:SYMBOL` value, converts numeric strings to numbers, converts ISO-8601 or Unix timestamps to UTC, preserves optional alert text and indicator values, and retains the original JSON as `raw_payload` for future agent adapters. Never include the webhook token or any secret in this JSON body.

### Local test

In one PowerShell window:

```powershell
$tokenBytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($tokenBytes)
$env:TRADINGVIEW_WEBHOOK_SECRET = [Convert]::ToHexString($tokenBytes).ToLowerInvariant()
python -m uvicorn app.main:app --no-access-log --host 127.0.0.1 --port 8000
```

In another PowerShell window:

```powershell
$body = @{ symbol = "OANDA:XAUUSD"; event = "price_alert"; price = "2648.32"; time = "2026-09-29T12:00:00Z" } | ConvertTo-Json
$webhookUrl = "http://127.0.0.1:8000/api/webhooks/tradingview/$($env:TRADINGVIEW_WEBHOOK_SECRET)"
Invoke-RestMethod -Method Post -Uri $webhookUrl -ContentType "application/json" -Body $body
```

## Phase 7A — Versioned Sentinel Policy and Risk Assessment Contracts

Phase 7A records the existing deterministic Sentinel rules in an immutable,
provider-neutral `SentinelRiskPolicy`. The policy has a deterministic semantic
fingerprint, an effective interval, an explicit fail-closed missing-data
strategy, and the existing settings already supplied to Sentinel: market
evidence age, approval TTL, requested-risk limit, specialist agreement, and
conflict handling. It introduces no new thresholds.

Each Sentinel evaluation produces an immutable `SentinelRiskAssessment` made
up of deterministic `RiskCheckResult` artifacts. The assessment preserves the
policy, proposal, evidence, evaluation-time, and check-result lineage. It does
not authorize a proposal, create an approval token, determine a trade
direction, calculate size, or invoke Pilot. `SentinelDecision` remains the
only final risk decision and now references the policy and assessment
fingerprints it evaluated.

Phase 7A does not bind Sentinel to a source account or derived portfolio
metrics (Phase 7B), introduce freshness or news/economic/corporate event-risk
assessment (Phase 7C), or change the process-local approval lifecycle (Phase
7D). Sentinel remains the sole approval authority and Pilot remains
`DISABLED_NO_EXECUTION`.

## Phase 7 — Production Sentinel Risk Policy

Phase 7 preserves Sentinel as FRIDAY's sole approval authority. Phase 7A adds
an immutable, versioned policy and deterministic assessment lineage. Phase 7B
adds optional policy-controlled binding to one exact source-scoped account,
portfolio snapshot, and compatible derived metrics snapshot; no account values
are inferred or reconciled across sources. Phase 7C adds policy-controlled
freshness and factual news, economic-event, and corporate-action evidence
checks. Missing, stale, future-dated, ambiguous, or incompatible required
evidence fails closed as `INSUFFICIENT_DATA`; explicit policy-limit breaches are
`REJECTED`.

Phase 7D makes the internal approval registry bounded and deterministic. It is
still process-local by design: after restart, approvals are unavailable and
therefore fail closed. It stores only the minimum Sentinel decision identities,
never provider payloads, article bodies, credentials, or execution data.

No Phase 7 component creates a trade direction, entry, stop, target, leverage,
or automatic sizing. Master and thesis remain non-authorizing. Pilot remains
`DISABLED_NO_EXECUTION`; there is no MT5, broker, or order-execution path.

## Phase 8 — Pilot Execution Architecture

Pilot is an execution coordinator, never a strategy or risk engine. It can
only verify a registered, unexpired, unconsumed Sentinel approval; without one
it creates no `ExecutionIntent`. Intent preparation copies only explicit
proposal values and preserves proposal, evidence, policy, assessment, and
account lineage. It does not consume approval: consumption is reserved for a
future successful adapter submission.

Intents are immutable, deterministically identified, and retained in a bounded
process-local store. Duplicate identical requests resolve to the same intent.
Phase 8 stops at the `ExecutionAdapter` protocol and returns
`ADAPTER_UNAVAILABLE`; no adapter is called. MT5/broker integration belongs to
Phase 9. Phase 10 will add execution safety, reconciliation, kill switch, and
order-state recovery. No MT5 package, credentials, broker session, symbol
mapping, sizing calculation, or order submission exists in Phase 8.

## Phase 9 — MT5 / Broker Adapter

Phase 9 adds an optional `MT5ExecutionAdapter` behind the existing
`ExecutionAdapter` protocol. Pilot still verifies the registered Sentinel
approval and creates the immutable `ExecutionIntent`; it only calls the
injected adapter's `prepare()` method and never imports MetaTrader5 directly.
The adapter imports the optional Windows `MetaTrader5` package lazily, so the
backend remains available when the package, terminal, local configuration, or
terminal session is unavailable.

MT5 settings are local-only and use process environment values before the
`backend/.env` fallback: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, optional
`MT5_PATH`, and optional `MT5_EXECUTION_MODE`. `backend/.env` is ignored by
Git; use `.env.example` placeholders only. Passwords are not included in
intents, results, logs, exceptions, or safe dry-run requests.

The adapter needs an explicit mapping such as `{"XAUUSD": "XAUUSD.a"}`.
It does not infer aliases or silently change a canonical symbol. Missing or
ambiguous mappings, unavailable/hidden/untradable symbols, absent quotes,
account-binding mismatches, invalid price precision, invalid volume steps, and
unsupported explicit order or filling modes all fail closed.

Execution modes are `disabled`, `read_only`, `dry_run`, and `live`; the default
is `disabled`. `read_only` supports terminal/account details, instrument
metadata, positions, and broker order status. `dry_run` validates the explicit
intent and builds a sanitized request without calling `order_send`. `live` is
also hard-blocked with `MT5_EXECUTION_DISABLED`: Phase 9 has no reachable live
submission path. Phase 10 is reserved for the separate execution-safety gate,
idempotency, reconciliation, kill switch, and recovery design.

## Phase 10 — Final Execution Safety

Phase 10 adds a provider-neutral execution-control layer around an existing
immutable `ExecutionIntent`. It records deterministic lifecycle states,
idempotency identity, approval/policy/assessment lineage, account identity,
requested values, safe broker identifiers, transitions, and reconciliation
status. The lifecycle permits only explicit state transitions; invalid changes
fail closed.

The final execution gate answers a different question from Sentinel:

- Sentinel: *is this proposal risk-approved?*
- Phase 10: *is this exact approved intent currently safe and authorized for
  submission?*

It checks exact Sentinel approval lineage, expiry, account identity, intent and
proposal lineage, duplicate/conflicting attempts, a bounded thread-safe state
store, a submission guard, adapter availability, and the kill switch. A
process-local `ExecutionKillSwitch` blocks new submissions when triggered. It
does not cancel broker orders or positions. When Pilot has an adapter, it
requires this final gate and cannot call the adapter directly.

The approval remains unconsumed for intent creation, validation, read-only MT5
operations, and dry runs. It would be consumed only after a future, confirmed
broker-submission boundary; no Phase 10 code crosses that boundary.

Reconciliation compares sanitized provider-neutral broker observations with an
execution record. Missing, unknown, or mismatched broker state becomes
`RECONCILIATION_REQUIRED`; it never triggers an automatic retry or replacement
order. The bounded store fails closed at capacity rather than evicting an
execution identity and weakening duplicate protection. Because records are
deliberately process-local, a restart cannot prove that an earlier broker order
was absent. Such an attempt must be
registered as stale and reconciled before any later submission.

Live execution remains disabled. The current final gate always returns an
execution block and no code path calls `ExecutionAdapter.submit()` or MT5
`order_send`. A broker reconciliation worker, production authorization, and
any live-submission capability remain future work.

## Production readiness / hardening

FRIDAY is hardened for controlled validation, not unrestricted live trading.
The safety path remains unchanged:

```
FRIDAY -> Sentinel -> Pilot -> ExecutionIntent -> Final Execution Gate -> adapter
```

The final gate still blocks broker submission. `MT5ExecutionAdapter.submit()`
remains a hard stop, and no component is permitted to call an MT5 order
submission API.

### Durable audit ledger

`app.audit` provides a provider-neutral, SQLite-backed append-only ledger for
safe references to execution lifecycle activity. It stores immutable event IDs,
UTC timestamps, proposal/intent/approval/execution references, state, reason
code, and fingerprints. It intentionally excludes passwords, API keys, tokens,
raw provider payloads, and broker authorization responses.

Set a local, access-controlled ledger path before any future
submission-capable deployment:

```powershell
$env:FRIDAY_AUDIT_DB_PATH = "C:\secure\friday\audit.sqlite3"
```

The ledger is idempotent for an exact event and fails closed for conflicts,
corruption, capacity exhaustion, or unavailable persistence. When supplied to
the final gate, a persistence failure blocks the safety boundary. It is not an
order database and does not authorize execution.

### Health, retries, timeouts, and observability

`app.health` records passive, bounded health observations for Alpha Vantage,
Twelve Data, TradingView webhook ingestion, MT5, the execution adapter,
Sentinel, and the audit store. Publishing health never creates provider traffic.
MT5 health reflects known adapter state only; a healthy connection is never a
trading authorization.

`app.services.retry_policy` produces deterministic retry plans. Only explicit
transient provider failures, timeouts, unavailability, and bounded rate-limit
responses are retryable. Broker submission, unknown broker outcomes,
authorization failures, invalid parameters, and Sentinel/risk rejections are
never retried automatically. Existing provider HTTP timeouts remain bounded.

`app.observability` emits structured, redacted logs using IDs and fingerprints.
It rejects or drops password, secret, token, API-key, credential, and
authorization fields. Do not add raw payloads or account details to log fields.

### Configuration and readiness

`validate_runtime_configuration()` checks non-secret operational settings:

- `FRIDAY_AUDIT_DB_PATH` must be configured for durable audit use.
- `FRIDAY_OPERATION_TIMEOUT_SECONDS` must be a finite value from 0 to 30.
- `MT5_EXECUTION_MODE` accepts only `disabled`, `read_only`, `dry_run`, or
  `live`.

`live` is deliberately rejected by production-readiness validation. Credentials
alone never activate execution. `OperationalReadinessChecker` combines valid
configuration, Sentinel and audit availability, passive component health, kill
switch state, and unresolved reconciliation state into one of:

- `READY_FOR_DRY_RUN`
- `READY_FOR_CONTROLLED_VALIDATION`
- `NOT_READY`
- `BLOCKED`

Every readiness report also states `LIVE_EXECUTION_BLOCKED`.

### Replay and recovery

`app.replay` is an engineering replay foundation. It deterministically orders
immutable references to evidence, specialist artifacts, proposals, Sentinel
decisions, intents, and execution-state transitions. It does not call a
provider, MT5, broker, or network, and it does not calculate strategy returns
or profitability.

Restart/recovery stays fail-closed: in-memory execution state cannot prove that
an earlier broker action did not occur, so it requires reconciliation. Unknown
or mismatched broker observations never trigger retry, replacement, position
closure, or automatic recovery.

### Controlled validation checklist

Before a future manual validation step, an operator must verify all of the
following:

1. Audit persistence is configured, writable, healthy, and monitored.
2. Sentinel, final execution gate, and kill switch are healthy; the kill switch
   is inactive.
3. No execution is stale or awaiting reconciliation.
4. MT5 is tested first in `DISABLED`, then `READ_ONLY`, then `DRY_RUN` only.
5. Account binding and explicit symbol mappings are validated without aliases.
6. The test uses fake/demo data and has an explicit human operator.

This checklist does **not** enable live trading. The final state of this phase
is: **production-hardened, ready for controlled validation, live execution
blocked**.

## Phase 11 — Controlled Demo Authorization

Phase 11 adds a narrow, immutable pre-authorization artifact for the first
future controlled demo order. It is an authorization record, **not** an order
or execution instruction. The current implementation creates it only when all
existing Sentinel, proposal, execution-intent, readiness, account, instrument,
market-freshness, risk-limit, kill-switch, and durable-audit checks pass.

The artifact is bound to one exact MT5 source account, the explicit
`XAUUSD -> XAUUSD` mapping, one `MARKET` order with `FOK` or `IOC`, exact
quantity, one registered unconsumed Sentinel approval, one immutable intent,
and an explicit short window. Its expiry is the earliest of the proposal,
Sentinel approval, controlled-demo policy, market freshness, intent, and
execution-window expiries.

The registry stores one identity only, is bounded, thread-safe, and begins in
recovery uncertainty. A future operator/recovery workflow must explicitly
confirm a clean state; a restart cannot silently recreate authority. Any
previous durable authorization lifecycle record causes a fail-closed recovery
block. Revoked, expired, consumed, or reconciliation-required artifacts cannot
be verified.

Every Phase 11 result keeps `submission_allowed=false`. The authorization
service imports neither Pilot nor an execution adapter and cannot call
`adapter.submit()` or MT5 `order_send()`. Sentinel approval remains
unconsumed during authorization and verification; consumption remains reserved
for a separately reviewed future submission boundary. `DEMO_CONTROLLED` and
live execution remain disabled.

## Phase 13 — Durable Reservation and Recovery

Phase 13 provides a **test-only**, SQLite-backed reservation and audit-outbox
foundation. A reservation row, its immutable lifecycle record, and its local
outbox event are committed in one SQLite transaction. Projection to the
existing audit ledger is intentionally idempotent, but is not described as an
atomic cross-database transaction.

The store has no default evaluator: construction fails closed unless an
explicit typed, test-only evaluator is supplied. This evaluator is **not an
authority boundary**: arbitrary Python code can implement it, and its result is
only a `test_only_validated` structural check. It cannot prove the current
state of any external or process-local source and can never create production
authorization or submission authority. A future production boundary requires
independently trusted integrations for the approval registry, controlled-demo
authorization store, broker instrument metadata, market freshness, and
portfolio-risk state.

The test-only evaluator structurally binds the exact reservation fingerprint
and complete safety-evidence fingerprint, including instrument limits, market
freshness, portfolio limits, Sentinel lineage, authorization lineage, account
binding, and expiry. These facts are rechecked before each readiness-increasing
transition. Missing, malformed, stale, or mismatched test output moves the
reservation to reconciliation-required when that state can be persisted; it
never advances the reservation.

Reservations use an explicit lifecycle and recover conservatively. An expired
pre-attempt reservation is persisted as `EXPIRED`; any unfinished or unknown
attempt is persisted as `RECONCILIATION_REQUIRED`. Recovery never retries,
replaces, or submits an order.

This is not live-execution-ready. The test-only reservation does not share an
atomic transaction with durable Sentinel approval ownership or the
process-local controlled-demo authorization store. Phase 13 does not import
Pilot, MT5, a broker adapter, or an API route. Global execution and
`DEMO_CONTROLLED` remain disabled.

### Phase 13A — Durable approval and demo-authorization state

Phase 13A adds a bounded SQLite lifecycle store for **internal** copies of
registered Sentinel approvals and issued controlled-demo authorizations. Each
record retains immutable source lineage, status, lifecycle version, timestamps,
and a local audit-outbox event in one SQLite transaction. Verification is
read-only; consume, revoke, expiry, and reconciliation-required transitions
are serialized and persisted.

This is intentionally a lifecycle foundation, not a replacement for the
shared durable Sentinel-approval owner or the process-local
`DemoAuthorizationStore`. No execution consumer reads from this store, and it
does not establish cross-store atomic consumption, broker authority, Pilot
access, adapter submission, MT5 access, or an API execution route. Missing,
corrupted, unsupported, or unavailable SQLite state fails closed. Reopening
the store preserves known terminal state; it never recreates expired, revoked,
consumed, or reconciliation-required authority.

### Phase 13C-1 — Shared durable authority schema bootstrap

Phase 13C-1 adds a separate, versioned SQLite schema intended to become the
future consolidation target for Sentinel approvals, controlled-demo
authorizations, execution reservations, lifecycle history, and a durable
outbox. It currently exposes only schema readiness diagnostics and a safe
bootstrap lifecycle/outbox primitive used to verify local SQLite transaction
behavior. It cannot register, verify, revoke, consume, or otherwise activate
an approval, authorization, reservation, Pilot path, adapter, MT5 connection,
or broker submission.

The database enables foreign keys on every connection, verifies the canonical
SQLite table, column, constraint, foreign-key, unique-index, and named-index
structure before reporting ready, rejects unsupported or incomplete migrations,
and has no in-memory fallback. Lifecycle and local outbox records are written
in one SQLite transaction. Projection to any future audit ledger is separate
and **at-least-once**: if the external projector succeeds but local delivery
acknowledgement fails, replay invokes it again. Projectors must deduplicate the
stable outbox ID when duplicate external effects are unacceptable; no
cross-database atomicity or exactly-once delivery is claimed. Existing
`ApprovalRegistry`, `DemoAuthorizationStore`,
`DurableAuthorityStore`, and test-only `DurableReservationStore` remain
disconnected active/inactive owners exactly as before this bootstrap phase.

### Phase 13B — Authority protocol and compatibility façade

Phase 13B adds typed lifecycle requests and deterministic result contracts for
approval and controlled-demo authorization operations. The
`DurableAuthorityProtocolAdapter` wraps the existing SQLite lifecycle store,
but every result is explicitly scoped as `DURABLE_LIFECYCLE_ONLY`: it is not
proof of a current Sentinel registry decision, a live demo authorization, or
broker state.

`ApprovalRegistry` and `DemoAuthorizationStore` now accept a protocol
candidate for dependency-injection compatibility, but do not call it. Existing
consumers therefore remain on their process-local owners, and the candidate is
reported as disconnected. Requests that require an authoritative durable
backend fail closed because no source-trusted backend exists yet. A later
reviewed phase must migrate approval, authorization, reservation, and outbox
state into one shared transaction boundary before any durable authority claim
is possible. The Phase 13B result contract rejects
`PRODUCTION_AUTHORITATIVE` at construction time: a model-shaped success result
cannot be promoted into authority by a caller. Phase 13B adds no execution
capability.

### Phase 13C-2 — Trusted Sentinel issuance and durable approval ownership

Phase 13C-2 makes the shared SQLite authority database the sole **durable
owner of Sentinel approvals**. The application composition root creates one
verified `SharedAuthorityStore`, injects it into the `ApprovalRegistry`
compatibility facade, and constructs the Sentinel gate around the
application-owned `TrustedSentinelIssuanceService`. Normal callers provide a
proposal, immutable evidence snapshot, and risk context; the service executes
the existing Sentinel evaluation and persists only an internally derived
`APPROVED` decision.

Approval issuance, the active lifecycle row, immutable lifecycle history, and
its local outbox event commit in one SQLite transaction. Verification is
read-only; consumption, expiry, and revocation use deterministic durable state
and survive restart. A malformed schema, unavailable database, failed
transaction, mismatched proposal/evidence/policy/assessment lineage, expiry,
or terminal lifecycle state fails closed. Legacy process-local approvals are
not adopted: an approval must be reissued by trusted Sentinel evaluation.

This is a controlled application architecture, not a cryptographic security
boundary against arbitrary hostile Python code in the same process. The public
issuance service does not accept caller-created `SentinelDecision` objects;
private storage hooks are reserved for the composition-owned service and are
not a public authority API. The Phase 13B protocol remains
`DURABLE_LIFECYCLE_ONLY` and is not a production verifier.

Controlled-demo authorization remains in its separate process-local store;
durable reservation, approval/authorization cross-store consumption, trusted
broker metadata, market freshness, portfolio-state verification, and external
broker reconciliation remain separate blockers. The outbox remains
at-least-once to external projectors. Phase 13C-2 imports neither Pilot nor an
execution adapter, does not initialize MT5, and does not enable demo or live
submission.

### Phase 13F — Read-only trusted execution-evidence boundary

Phase 13F adds a provider-bound, read-only validation boundary for instrument
constraints, market references, account/position snapshots, and validated
portfolio-risk input lineage under their exact account and symbol scope.
`FridayCore` composes one
`TrustedExecutionEvidenceService`; its default provider is explicitly
unconfigured and returns no observations. It therefore reports evidence as
unavailable rather than using caller-supplied readiness-context values,
synthetic prices, or a cached fallback.

Configured providers must return typed observations with their configured
provider identity. The service calculates deterministic fingerprints from
validated data, rejects stale/future observations, symbol/account mismatches,
invalid broker constraints, inconsistent positions, malformed responses, and
provider failures. A validated result is still non-authorizing: it cannot
issue approvals, authorizations, reservations, or submissions. The boundary is
application-level provenance validation, not cryptographic protection against
hostile code already executing inside the process.

Portfolio-risk input lineage binds the exact source-position snapshot and a
policy fingerprint, but it deliberately contains no `safe` assertion. A later
trusted policy/risk service must independently evaluate those validated inputs
before any execution-readiness claim is possible.

No live read-only broker provider is configured in this repository. Controlled
fakes exist only in isolated tests. When trusted evidence is required,
operational readiness reports `TRUSTED_EXECUTION_EVIDENCE_UNAVAILABLE` instead
of reporting execution readiness. Durable demo authorization, reservations,
external broker reconciliation, and all demo/live submission remain disabled.

The controlled-demo authorization service now requests this evidence internally
for every create and verification operation. Its legacy caller-provided
`DemoReadinessContext` is comparison-only: conflicting account, instrument,
position, or market facts are rejected and cannot override provider-bound
observations. The default unconfigured provider blocks authorization with
`DEMO_TRUSTED_EVIDENCE_UNAVAILABLE`; successful test paths inject explicit
test-only providers. The durable reservation store remains separately
test-only because neither a real evidence provider nor atomic durable
authorization-and-reservation consumption is available. Recovery therefore
continues to fail closed and requires reconciliation rather than retry.

### Passive MT5 evidence and Sentinel portfolio-risk verification

`MT5ReadOnlyExecutionEvidenceProvider` is an opt-in provider-specific adapter
for a runtime that is already connected by another explicitly authorized
owner. It calls only the adapter's passive evidence snapshot method; it never
initializes MT5, logs in, selects a symbol, calls an order method, or changes
terminal/account state. A disconnected runtime returns an explicit unavailable
result. The default FRIDAY composition remains the no-I/O unavailable provider.

The passive snapshot records sanitized account, exact mapped instrument,
bid/ask, source quote time, retrieval time, positions, and pending-order facts.
It intentionally does **not** claim that MT5 establishes Sentinel policy or
portfolio-risk correctness. `SentinelPortfolioRiskEvidenceVerifier` separately
recomputes the existing controlled-demo policy's volume, monetary-risk,
notional, position-count, aggregate-risk, price-direction, account, instrument,
currency, proposal, intent, approval, and trusted-evidence bindings. Its
immutable result is non-authorizing and fails closed for missing, stale,
mismatched, or unverifiable input.

No real terminal was initialized to test this phase. Tests use strict fakes
that fail if initialization, login, or `order_send` is reached. Even with an
already-connected read-only source, live/demo submission remains disabled; a
future shared atomic authority/reservation/consumption transaction and an
authoritative portfolio-risk input source are still required.

### Sentinel portfolio-risk policy governance

The controlled-demo portfolio-risk calculator now resolves an exact immutable
policy version through `SentinelPortfolioRiskPolicyVerifier` before it uses any
risk limit. The canonical policy fingerprint covers the existing account,
instrument, currency, order/filling, volume, exposure, monetary-risk, notional,
freshness, effective-window, and calculation-semantics fields. The verifier
recomputes this fingerprint and binds a successful non-authorizing risk record
to the exact policy source, approval reference, calculation version, proposal,
intent, Sentinel decision, account, instrument, positions, and market evidence.
Caller-provided limits and hashes are comparison inputs only and cannot replace
source-resolved content.

This repository does **not** currently contain an independently verifiable
policy-approval authority. Sentinel's trusted issuance service owns trade
approval decisions; it does not approve controlled-demo policy versions. The
production policy source is therefore unavailable by default, and controlled-
demo authorization fails closed with an explicit policy-authority diagnostic.
A hash, local database row, environment variable, caller flag, or Python
protocol implementation is not treated as policy approval.

`LocalSentinelPolicyCatalog` provides restart-safe local history for canonical
policy versions, revocation, and atomic supersession. It enforces one active
version per policy identity, immutable version content, explicit effective
intervals, SQLite foreign keys, strict schema verification, and durable
lifecycle events without an in-memory fallback. Its records are permanently
labelled `LOCAL_GOVERNANCE_ONLY` and are not accepted as execution authority.
Test-only sources require an explicit test factory and remain non-authorizing;
selecting a test source through the normal verifier fails closed.

#### Independent policy-approval boundary

`SentinelPolicyApprovalArtifact` is an immutable `sentinel-policy-approval/v1`
claim bound to the exact policy ID, policy version, canonical policy
fingerprint, approver identity, validity interval, and account/mode/instrument/
currency scope. Its deterministic payload fingerprint deliberately excludes
the proof bytes; the artifact ID binds both that canonical payload and the
proof. Detached-signature claims must identify the verification key and the
supported signature algorithm. Constructing or storing any artifact proves
only content integrity—it does not establish the approver's identity or
authority.

Approval lifecycle is represented separately as immutable active, revoked, or
superseded state. Policy resolution checks exact identity/content, effective
time, lifecycle, and scope before risk calculations. Readiness reports policy
source, trust-anchor, artifact, approver, fingerprint, lifecycle, scope, and
production-resolution availability as separate safe booleans; partial
readiness can never advertise production availability. Risk evidence retains
the approval artifact ID, canonical artifact fingerprint, approver ID, and
policy binding without granting execution authority.

There is currently **no authorized independent policy approver, approved public
verification key, key-management integration, or external governance service**
configured for FRIDAY. Consequently the production approval verifier is always
typed `UNAVAILABLE`, and production policy resolution remains blocked. A local
catalog row, Git hash, environment value, trade-proposal approval, approval-
shaped object, approver name, or signature-shaped string is not a trust anchor.
No current policy approval artifact has been manufactured.

Tests define their fake verifier inside the test suite and can reach only
`TEST_ONLY_VERIFIED`; that status remains non-authorizing and cannot advertise
production readiness. Normal construction rejects the test policy source.
Activation requires an explicit governance decision naming the authorized
approver and scope, plus a reviewed production verifier backed by that
authority's established public-key/KMS or attestation trust configuration.
Private signing material must remain outside the repository. Revocation and
replacement also require an authoritative durable source; the existing local
policy catalog remains history-only. Atomic policy approval, trade approval,
demo authorization, reservation, and consumption remains unresolved.

#### Formal governance and trust configuration

`SentinelPolicyGovernanceRecord` defines the governance decision that must
exist before an approval verifier can become authoritative: named approver and
organizational role, approval/revocation capabilities, exact policy/environment/
account/instrument scope, validity and lifecycle, audit responsibilities, and
the required trust-anchor mechanism. `UNCONFIGURED`,
`LOCAL_DECLARATION_ONLY`, and `TEST_ONLY` records are explicitly non-
authoritative. An `INDEPENDENTLY_VERIFIED` record requires a non-test approval
mechanism and a public trust-anchor descriptor whose identity and provenance
were established outside the repository. Model construction, a local row, or
a matching fingerprint does not prove that external establishment occurred.

The typed `SentinelPolicyGovernanceSource` and
`SentinelPolicyApprovalLifecycleSource` contracts separate the governance
decision from the current approval/revocation record. Production composition
uses only unavailable implementations. Arbitrary protocol implementations are
reported as untrusted rather than inferred to be authority. Readiness now
distinguishes governance-contract configuration, authoritative approval-source
configuration, current revocation availability, trust-anchor verification,
artifact availability, approver verification, fingerprint binding, lifecycle,
and scope. Every item is required for production policy resolution.

The unsigned decision template is
[`docs/sentinel-policy-governance-decision.template.md`](docs/sentinel-policy-governance-decision.template.md).
Completing or committing it cannot authorize a policy. A legitimate decision-
maker must approve the named approver and scope, establish the public trust
identity independently, define key custody or external-service ownership, and
name the durable approval/revocation source. A separate reviewed integration is
then required; production startup cannot select a fixture or local catalog.

Current transaction boundaries remain intentionally separate: governance and
policy catalogs are history/configuration boundaries; Sentinel trade approval
uses the shared authority SQLite store; controlled-demo authorization,
reservation, audit projection, and broker reconciliation retain their existing
owners and guarantees. The repository does not claim atomicity across those
stores. A crash or ambiguity between stores must continue to fail closed and
require reconciliation. Consolidation into one tested SQLite transaction may
be feasible only after an authoritative policy source exists and migration can
preserve existing records, ownership, outbox, revocation, and recovery rules.

Tests use synthetic policy and execution evidence only. They do not initialize
MT5, connect to a broker, or call an order API. Live and controlled-demo
submission remain disabled. The production approval, demo-authorization,
reservation, and consumption cutover remains unresolved, and production use
also requires a genuine independently approved policy source.

### Shared non-submitting local lifecycle transaction

The shared authority store now contains a private, explicitly test-only local
transaction that demonstrates the safely achievable SQLite boundary without
claiming production authority. After an existing application service has
created a controlled-demo authorization, the store can persist its exact
immutable lineage under the already-durable Sentinel approval. A subsequent
test-only reservation transaction validates the exact approval,
authorization, proposal, policy, evidence, account, server, symbol, intent,
quantity, order type, filling mode, expiry, and nested safety-evidence
fingerprints. It then creates the reservation and consumes both local authority
records in one `BEGIN IMMEDIATE` transaction with optimistic lifecycle checks,
foreign keys, lifecycle history, and outbox events. A commit failure rolls back
all three state changes; concurrent identical requests are deterministic and
cannot consume authority twice.

This primitive is intentionally underscore-prefixed, accepts only the frozen
`DurableExecutionReservation` contract whose `test_only` flag is the literal
`True`, and is not composed into the authorization service, Pilot,
`FinalExecutionGate`, an adapter, an API route, or startup execution flow. It
does not make the separate audit ledger or any external broker operation part
of the SQLite transaction. External outbox projection remains at-least-once.

Restart recovery is conservative: expired local authority becomes terminal,
while any locally ready/started reservation becomes
`reconciliation_required` unless already expired. Recovery never infers an
external order outcome and never retries or creates replacement authority.
There is still no authoritative broker-state reconciliation provider configured
by default.

The local transaction machinery therefore closes a storage/concurrency gap but
does **not** make execution ready. Controlled-demo authorization remains on its
existing process-local owner in application composition, and the shared
transaction is not activated. Production policy governance, trust anchor,
authoritative approval/revocation source, trusted live evidence, durable demo-
authorization cutover, and reviewed submission-boundary integration remain
external or future dependencies. Live and demo submission remain disabled.

### Durable policy-approval and revocation history

`LocalSentinelPolicyApprovalCatalog` is a restart-safe SQLite catalog for
policy-approval claims and their lifecycle history. It binds each immutable
claim to the exact policy ID, version, canonical policy fingerprint, approver
claim, scope, validity interval, and deterministic artifact/state
fingerprints. Registration is idempotent; conflicting active claims are
rejected by a database uniqueness constraint. Revocation and supersession use
explicit `BEGIN IMMEDIATE` transactions, optimistic lifecycle checks,
immutable deterministic events, foreign keys, strict canonical-schema
verification, and rollback on commit failure. Reopening the database preserves
terminal lifecycle state, so an old approval artifact cannot silently become
active again.

This catalog is intentionally **not** an approval authority. It records what
local software was given; it cannot prove who supplied the claim, whether that
person was authorized, or whether a signature or external attestation is
legitimate. `NonAuthoritativeLifecycleApprovalVerifier` can expose durable
revocation, supersession, expiry, and conflict information to fail-closed
policy checks, but it always returns `UNAVAILABLE` and can never return an
authoritative or test-verified result. Caller-supplied sources therefore cannot
promote catalog rows into production policy authority.

Production policy resolution remains blocked until the organization names a
legitimate approver, independently establishes and reviews a trust anchor, and
configures an authoritative approval/revocation source capable of verifying
the exact approval artifact. That external authority is not invented or seeded
by this repository. The current shared execution lifecycle is not wired to the
local policy catalog because doing so would incorrectly elevate durable local
storage into independent governance authority. Tests can be run without MT5:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_sentinel_policy_approval_catalog.py -q
```

Passing these tests demonstrates storage, concurrency, and fail-closed
lifecycle behavior only. It does not enable controlled-demo or live execution,
provide authoritative broker reconciliation, or establish governance approval.

### Phase 13G production-authorization readiness diagnostic

`ProductionAuthorizationReadinessGate` is the single read-only diagnostic for
the current production-authorization boundary. It reports every prerequisite
independently: authorized policy approver and scope, independent trust anchor,
authoritative approval and revocation sources, exact policy integrity, trusted
execution evidence and freshness, Sentinel risk verification and durable trade
approval, durable reservation, audit durability, kill-switch state, broker
reconciliation, submission-boundary review, explicit production authorization,
and execution mode.

The report is immutable, deterministic, and structurally non-authorizing. Its
status is always `blocked`; `production_authorization_available`,
`submission_allowed`, `execution_authority`, and each check's
`authorizes_execution` field are literal `False`. A test-only policy resolution
may demonstrate policy-content integrity, and a durable Sentinel trade approval
may be verified without consumption, but neither can satisfy the missing
organizational governance or production cutover requirements. Current durable
reservations are explicitly test-only and are therefore reported as blocked.
Every satisfied check requires explicit source provenance, every diagnostic
code uses a canonical bounded identifier, and all checks share the report's
single UTC evaluation timestamp. This prevents a persisted or reviewed report
from combining observations made at different assessment times. Policy failure
classification uses explicit reason-code mappings rather than substring
inference, so unrelated diagnostics cannot be mislabeled as revocation or
supersession.

The diagnostic does not initialize MT5, retrieve provider data, consume an
approval, mutate lifecycle state, invoke Pilot or an adapter, or submit an
order. It is not exposed as a public API. Missing, invalid, stale, revoked,
superseded, conflicting, and unresolved inputs remain distinct failure states.
No hash, local SQLite record, Git commit, test fixture, unsigned template, or
protocol-shaped object is promoted into authority.

Production remains blocked because FRIDAY has no configured authorized policy
approver, independently established trust anchor, authoritative policy
approval/revocation service, production-capable durable reservation contract,
authoritative broker reconciliation source, reviewed submission boundary, or
explicit production execution authorization. The trusted execution-evidence
provider is unavailable by default, and live and controlled-demo submission
remain disabled. The unsigned governance decision template records the real
human and infrastructure decisions required to close those gaps; completing
the template alone does not authorize execution.

### Phase 13H independent-governance integration contracts

`IndependentPolicyAuthorityIntegration` and
`BrokerReconciliationIntegration` define the read-only, provider-neutral
request/observation boundary for the two external dependencies that cannot be
created by this repository. Requests bind exact policy or execution lineage,
account and instrument scope, a bounded freshness interval, and a per-request
challenge. Sanitized observations add provider identity, monotonic source
sequence, stable source event and audit references, timestamps, and explicit
trust-anchor rotation lineage. Broker observations also bind the exact account,
server, symbol mapping, and broker order identity.

These integrations perform structural validation only. Their immutable result
models make independent authority, production authorization, execution
authority, and retry permission literal `False`; a test double or arbitrary
protocol implementation therefore cannot authorize production. The default
providers report unavailable. The services do not mutate approval, reservation,
reconciliation, or execution state and are not composed into Sentinel, Pilot,
`FinalExecutionGate`, MT5, an adapter, or an API route.

This contract does not authenticate a provider, validate a real signature,
persist a replay window, operate a key registry, or prove broker truth. A real
integration still requires the independently governed decisions and external
infrastructure listed in
[`docs/phase13h-independent-governance-integration.md`](docs/phase13h-independent-governance-integration.md).
Until those dependencies are configured and independently reviewed, the Phase
13G readiness diagnostic remains blocked and live and controlled-demo
submission remain disabled.
