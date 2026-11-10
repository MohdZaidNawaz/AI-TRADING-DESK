# Phase 5.4 — Evidence-driven agent coordination

## Scope

Phase 5.4 extends the existing FRIDAY Core. It does not introduce a second agent
framework, an AI model, provider calls, broker connectivity, or execution authority.
The legacy `AgentAnalysis.status` field remains for API compatibility. The additive
`execution_state` and `coordination` fields are the authoritative runtime telemetry.

## Specialist capabilities

| Seat | Role | Current evidence-backed capability | Honest limitation |
|---|---|---|---|
| Chartist / Oracle | Technical / quantitative | Validated OHLCV-derived features and technical reports | Unavailable without validated historical series |
| Atlas | Macro | Phase 5.3 deterministic macro facts, calculations and conditional interpretations | Unavailable without canonical macro evidence |
| Scout | News and session context | Validated news/economic evidence, timestamps and provider status | A headline is not causation; session inference requires validated time evidence |
| Capitol | Liquidity | Routed market and positioning evidence | Order blocks, FVG, BOS and CHoCH are not claimed without implemented calculations |
| Athena | Strategy research | Dependency metadata and evidence-backed conditional interpretation | No automatic signal or invented scenario |
| Sentinel | Risk | Routed volatility, portfolio-risk and missing-data context | No authorization decision is created by analysis |
| Ledger | Journal / provenance | Current-run lifecycle and source references | Historical user trade records remain unavailable unless supplied |
| Pilot | Execution control | Explicit disabled-state reporting | Always unavailable for execution; no order path is enabled |
| Master | Synthesis | Structured agreement, conflict, missing-specialist and evidence summaries | Does not manufacture consensus or predictions |

## Coordination policy

One immutable task plan is created per correlation ID. Duplicate agent IDs are
rejected before execution. Independent tasks run in a bounded thread pool. Athena,
Sentinel, Pilot and Ledger wait for their declared predecessor tasks; unavailable
predecessors are retained as missing inputs rather than being silently replaced.
Final results are always returned in registered desk order.

Each execution records `pending`, `running`, and one terminal state:
`completed`, `partial`, `failed`, or `unavailable`. Timeouts discard late output and
do not retry. `cancel_analysis()` marks the active correlation for cancellation;
output arriving after the bounded wait is discarded. Adapter exceptions are reduced
to a safe category and never appear as completed.

## Evidence quality and synthesis

Specialist results expose bounded source/event references, observation timestamps,
calculation method IDs, output kinds, limitations, and missing inputs. Retrieval time
and freshness stay `null`/`unknown` where the underlying reference does not carry
them; they are never invented. Output kinds distinguish verified observations,
deterministic calculations, interpretations, and unavailable output.

The structured Master synthesis remains the source of agreement and conflict. It
does not coerce `conflicted` or `insufficient_data` into agreement. Conditional
bullish or bearish research scenarios are emitted only when at least two structured
specialists reach the same supported directional stance. They are explicitly not
trade instructions. Missing specialists and evidence gaps remain visible.

## Safety boundary

The coordination report structurally fixes `production_authorization_available`,
`submission_allowed`, and `execution_authority` to `false`. Phase 5.4 makes no live
provider request, does not read credentials, and does not add MT5, broker, order,
submission, or execution wiring.
