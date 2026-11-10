# Phase 5.2 macro provider integration

## Safety and operating model

Phase 5.2 adds typed, offline-testable provider adapters and a cached-evidence macro
assessment. It does not start a scheduler, make a provider request during analysis,
authorize production, submit orders, or grant execution authority. Retrieval occurs
only when an owner explicitly calls `MacroRetrievalService.refresh()` or
`historical_bootstrap()`.

All adapters use a bounded HTTP transport with certificate verification, an explicit
timeout, `trust_env=False`, a two-megabyte response limit, safe error categories, and
no automatic retry. A validated `Retry-After` value may be reported, but is never
acted on automatically. The transport never retains response bodies, request URLs,
headers, or credentials in errors.

Exact duplicate retrieval operations are single-flight coordinated. Existing valid
evidence remains in the bounded process-local evidence stores when a provider is
unconfigured, unavailable, rate-limited, malformed, or returns no observations.
This is cache preservation, not durable persistence: a process restart loses this
cache unless a separately reviewed persistent store is added.

## Provider status

| Provider | Status | Coverage | Required configuration | Known limitation |
|---|---|---|---|---|
| FRED/ALFRED | Implemented, opt-in | CPI, core CPI, PCE, core PCE, payrolls, unemployment, wages, real GDP, retail sales, effective federal-funds rate; vintages and point-in-time observations | `FRED_API_KEY` and `fred` in `FRIDAY_MACRO_ENABLED_PROVIDERS` | FRED is the retrieval provider; the original publisher remains separately identified. FRED terms still govern downstream redistribution. |
| U.S. Treasury | Implemented, opt-in | Official nominal 1M/3M/6M/1Y/2Y/5Y/10Y/20Y/30Y and real 5Y/10Y/30Y par curves | `us_treasury` in enabled providers | Exact publication time is unknown; the observation date is not relabelled as publication time. |
| BLS | Implemented, opt-in | CPI, core CPI, payrolls, unemployment, average hourly earnings | `bls` in enabled providers; optional `BLS_API_KEY` selects v2, otherwise v1 | M13 annual averages are intentionally not normalized as monthly observations. Footnotes are signalled in diagnostics but not interpreted. |
| BEA | Typed disabled/unavailable adapter | Intended GDP/PCE/national-accounts boundary | `BEA_API_USER_ID` plus explicit enablement | Dataset, table, line-code, unit-multiplier, and revision mappings have not been independently verified. Metadata discovery is explicit; data retrieval stays fail-closed. |
| Federal Reserve | Partially implemented, opt-in | Official monetary-policy RSS documents | `federal_reserve` in enabled providers | Released document provenance is implemented. The tentative FOMC meeting calendar and decision-value extraction remain unavailable because no stable typed source schema has been verified. No hawkish/dovish inference is made. |
| Trading Economics | Typed disabled-by-default adapter | Intended scheduled events and consensus provenance boundary | Credential plus explicit commercial and redistribution authorization and provider enablement | Schema entitlements, exact mappings, and redistribution rights remain unverified, so retrieval remains unavailable even when configured. No fallback is attempted. |

PMI and DXY are unavailable. No licensed PMI series or exact validated DXY series and
redistribution entitlement is configured; unrelated proxies are not substituted.

## Pinned mappings and provenance

`macro_provider_registry.py` is the auditable registry. Enabled mappings contain an
exact provider series identity, canonical FRIDAY indicator identity, original
publisher, source role, licensing class, and vintage capability. Blocked mappings
have no invented provider identifier.

Normalized observations preserve reference periods, units, scales, seasonal
adjustment, retrieval time, available publication/effective/vintage times, and
revision lineage. Economic events distinguish the retrieval provider and original
publisher and can retain separate provenance for actual, forecast, previous, and
revised-previous values. A consensus forecast is never relabelled as an official
observation or actual release.

## Analysis and presentation

The macro service reads only an immutable evidence snapshot. It reports provider
availability, release surprise/revision arithmetic, indicator changes, the 2Y/10Y
nominal spread, available real yields, missing indicators, freshness, and explicit
limitations. It separates verified facts from interpretations and does not claim
price causation.

Atlas consumes the verified cached facts. The Master report summarizes provider
status and repeats the non-causation and non-authorizing boundary. The API adds the
macro report as an optional field, so responses without canonical macro evidence
retain their previous shape. The frontend renders the report only when present and
does not display stale values as current or forecasts as actuals.

The technical, news, risk, and strategy specialists retain their existing evidence
boundaries. Macro evidence can inform their shared context but cannot override
conflicting evidence or change any literal-false authorization, submission, or
execution field.

## Offline validation scope

Provider tests use mocked JSON/XML and transports. They cover revision lineage,
publisher/retrieval-provider separation, Treasury timestamps, BLS monthly versus
annual-average handling, Federal Reserve document provenance, commercial-provider
disablement, TLS and environment isolation, rate limits, Retry-After, timeout-stage
classification, no retry, single-flight retrieval, and cache preservation. Existing
macro contracts, series stores, economic events, agent/API regressions, compilation,
and frontend lint/build are also validation targets.

No live provider verification is part of Phase 5.2.
