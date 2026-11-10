# Phase 5.1: Macro Intelligence policies and contracts

Phase 5.1 is an offline, provider-neutral foundation. It adds no provider,
network request, API route, persistence, trading signal, authorization, or
execution capability.

## Canonical indicators

`CANONICAL_MACRO_INDICATORS` defines internal identities for US CPI/Core CPI,
PCE/Core PCE, payrolls, unemployment, hourly earnings, GDP, retail sales,
manufacturing/services PMI, Fed/ECB/BoE/BoJ policy rates, nominal US Treasury
yields (1M, 3M, 6M, 1Y, 2Y, 5Y, 10Y, 20Y, 30Y), real US Treasury yields (5Y,
10Y, 30Y), and US Dollar Index context. These are FRIDAY identities, not
provider IDs. Each definition fixes geography, currency, frequency, unit,
scale, seasonal-adjustment expectation, reference-period kind, source type,
and freshness policy.

Level definitions are intentionally explicit. For example, CPI is an index
level and GDP is a real level; percentage release variants must use separate,
future canonical identities rather than being mixed with these series.

## Freshness

Freshness is policy-specific and inclusive at each boundary:

| Policy | Fresh | Aging | Basis |
| --- | ---: | ---: | --- |
| Daily yields/dollar context | <= 3 days | <= 7 days | publication or source observation |
| Monthly observations | <= 45 days | <= 75 days | publication |
| Quarterly observations | <= 110 days | <= 200 days | publication |
| Policy rates | <= 45 days | <= 120 days | effective time |
| Scheduled events | <= 24 hours | <= 72 hours | retrieval age plus a future schedule |
| Released events | <= 2 days | <= 7 days | release time |

Older evidence is stale. Missing required timestamps are unknown; absent
evidence is not available. A recent local retrieval never refreshes an old
observation. Policy-rate aging is deliberately conservative: it requests
reconfirmation and does not assert that the underlying rate changed.

## Provenance and chronology

Source policies distinguish official observations, commercial observations,
central banks, economic calendars, commercial consensus, and
market-price-derived context. Actual,
forecast, previous, revised-previous, observed, and effective values remain
separate roles. Only a declared commercial-consensus source may establish a
consensus forecast; no provider is selected or configured by this phase.

`MacroEvidenceAvailability` records reference, publication, effective,
retrieval, and vintage time. Historical analysis may not use evidence before
its required publication/effective/vintage time. Revisions require vintage
availability, preventing a revised value from being used before the revision
was available. Invalid chronology and missing required bases fail validation.

## Assessment envelopes

Typed envelopes exist for release surprise, revision, indicator change,
policy-rate differential, yield-curve spread, real-yield context, cross-market
observation, instrument-specific assessment, provider diagnostic, and the
overall report. They carry status, method/version, evidence IDs, calculation
time, missing inputs, warnings, limitations, and evidence-completeness
confidence. No Phase 5.1 service calculates these values.

Gold, Silver, and Forex policies define required assessment categories only.
They do not encode direction, causality, trade recommendations, or authority.
All report authority fields are literal `false`.
