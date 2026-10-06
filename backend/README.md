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

Approved decisions are registered only in a lock-protected, process-local approval registry. Each approval is bound to the proposal ID and fingerprint, analysis ID, evidence fingerprint, evaluation time, and expiry. Re-evaluation is idempotent, and a Pilot hand-off claim is atomic and single-use. Restarting the backend clears this registry and invalidates every approval.

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
