# Phase 5.3 deterministic macro intelligence

## Scope

Phase 5.3 evaluates already validated, process-local macro and economic-event evidence. It does not retrieve provider data, call an AI model, create trading signals, or grant authorization. Provider retrieval remains an explicit Phase 5.2 operation.

## Availability and time rules

- Every assessment has an explicit UTC `evaluated_at` time.
- An observation is eligible only when its required publication, effective, or vintage time and its retrieval time are no later than `evaluated_at`.
- Revisions are selected only after their vintage is available. A future revision cannot hide an earlier version that was available at the evaluation time.
- The canonical indicator definition controls frequency, units, scale, seasonal adjustment, geography, currency, maturity, and reference-period kind.
- Fresh, aging, stale, unknown, missing, incompatible, and conflicting evidence remain distinct. Retrieval time never refreshes an old source observation.
- Source conflicts are retained as evidence IDs; one source is selected deterministically for calculations, with direct official sources preferred where otherwise comparable.

## Deterministic calculations

All arithmetic uses the exact decimal values in typed evidence and requires identical units and scales.

- Release surprise: `actual - forecast`. A zero forecast is a real value; a missing forecast is insufficient evidence. Direction is `above`, `below`, or `in_line`.
- Release revision: `revised_previous - previous`. Direction is `rising`, `falling`, or `unchanged`.
- Period change: `current - previous` for two chronological observations from the same canonical series. It is an absolute change, not a percentage growth rate.
- Policy differential: `base policy rate - quote policy rate`, using fresh or aging effective-rate observations for Fed, ECB, BoE, and BoJ combinations.
- Yield spread: `long maturity yield - short maturity yield`. Phase 5.3 exposes 3M-10Y and 2Y-10Y only when both observations have the same date.
- Real-yield context uses direct 5Y, 10Y, or 30Y real Treasury observations. It never synthesizes a real yield by subtracting unrelated inflation evidence.

Stale or unknown inputs never produce a calculated output. Incompatible units, scales, or observation dates fail closed.

## Instrument assessments

Gold, Silver, and Forex assessments use the Phase 5.1 factor policies. Each result includes supporting evidence IDs, assessed and missing factors, freshness, conflicts, confidence, verified facts, conditional implications, and limitations.

The implications are deliberately non-directional:

- Gold inflation context requires separate policy-rate, real-yield, nominal-yield, US-dollar, and risk evidence.
- Silver additionally requires industrial-growth context.
- Forex requires both currency policy observations, their differential, and currency-specific release evidence.

An incomplete factor set is `insufficient_data`; it is never filled with a default value. Neither the report nor Atlas nor Master produces a buy/sell instruction.

## Agent and API behavior

Atlas reads the cached report, summarizes verified facts and calculated period changes, and carries missing factors, conflicts, and limitations forward. Master reports macro calculation counts and conflict references separately from interpretation. Because the macro engine does not emit a directional trading view, Master does not infer specialist agreement or override contradictory technical evidence.

The existing `/api/analyze` response is extended additively. The frontend displays release lineage, trend periods and directions, policy differentials, yield observation dates, direct real yields, conditional implications, conflicts, missing inputs, freshness, and provider provenance. The Phase 5.2 `analysis_mode` value is retained for response compatibility.

## Remaining limitations

- No provider is contacted during analysis. Reports depend on evidence already ingested into the process-local store.
- Cross-market context is limited to actual cached US Dollar Index evidence; DXY, PMI, risk, or price relationships are never invented.
- The engine calculates absolute period changes, not seasonally adjusted annualized growth, diffusion-index regimes, or market reaction.
- Calendar forecasts remain commercial/consensus evidence and must carry field-level provenance.
- All production authorization, submission, and execution fields remain literal `false`.
