# Phase 5.6 — Deterministic Risk and Strategy Intelligence

## Boundary

Phase 5.6 is offline research and decision support. It does not select a trade,
approve a proposal, connect to an account, submit an order, or enable Pilot.
Production authorization, submission, and execution authority are literal
`false` in the report contract.

The engine reuses validated technical, liquidity, macro, news, event, and OHLCV
evidence. Missing evidence remains explicit. It never invents a current price,
contract specification, account balance, currency conversion, spread, commission,
calendar event, target, or directional conclusion.

## Risk and sizing formulas

For an evidence-bound entry reference `E`, invalidation/stop candidate `S`, and
target `T`:

- stop distance = `abs(E - S)`;
- potential reward = `abs(T - E)`;
- risk-to-reward = `potential reward / stop distance`;
- pip distance = `stop distance / verified pip size`, when pip size exists;
- risk amount = `verified account equity × risk percentage / 100`;
- monetary loss per size = `(stop distance / tick size) × tick value × verified
  currency-conversion rate`;
- raw size = `risk amount / monetary loss per size`;
- final size is rounded down to the verified size step and capped by the verified
  maximum.

Sizing is unavailable if entry/stop distance, tick size, tick value, monetary
currency, minimum/maximum/step, account equity, risk percentage, or required
currency conversion is absent or invalid. Lots are never assumed: the verified
specification states whether the size unit is lots, contracts, or units.
Contract size is used only to display underlying units. Transaction cost is
reported only when a verified spread or commission is supplied.

## Volatility

ATR 14 and rolling standard deviation 20 come from the existing deterministic
technical engine over completed validated candles. ATR divided by the latest
completed close produces a transparent descriptive regime:

- low: below 0.5%;
- normal: 0.5% through 1.5%;
- high: above 1.5%.

A latest candle range above 1.5 ATR is marked as range expansion. These thresholds
are deterministic policy bands, not statistically calibrated forecasts. Missing
interval warnings are retained. Scenario stop distance is also expressed as an
ATR multiple when both values exist.

## Scenario construction

The report always models bullish, bearish, and neutral/no-trade outcomes when an
assessment runs. It never selects one.

Directional entry zones are chosen in this order:

1. a non-invalidated same-direction order-block candidate;
2. a non-filled same-direction fair-value gap;
3. a confirmed support candidate for bullish research or resistance candidate
   for bearish research.

The opposing zone boundary is the invalidation candidate. A point level requires
another confirmed level beyond it before positive stop distance exists. Targets
come from opposing confirmed technical levels and relevant BSL/SSL candidates.
If an entry, stop, or target cannot be supported by evidence, the corresponding
calculation remains unavailable. Neutral/no-trade is an explicit valid outcome.

Every scenario retains supporting evidence, conflicts, missing inputs,
reassessment conditions, and limitations. Macro context is not converted into a
directional vote unless a future typed policy can do so without interpreting
free text. Scheduled event risk is treated as caution, never as proof of direction.
No verified session-calendar provider currently exists, so session context stays
missing.

## Strategy quality

The score measures evidence completeness, not expected return, win probability,
or profitability. Weights total 100:

| Factor | Weight |
| --- | ---: |
| Multi-timeframe agreement | 15 |
| Technical confirmation | 20 |
| Liquidity confirmation | 15 |
| Macro context | 10 |
| News/event risk context | 10 |
| Entry/invalidation clarity | 15 |
| Risk-to-reward suitability | 15 |

Each factor receives 0, 0.5, or 1 under documented deterministic conditions.
Scores at least 80 are labelled high evidence sufficiency, 60–79.99 medium,
35–59.99 low, and below 35 insufficient. A high score does not authorize a trade
and does not imply that the scenario will succeed.

## Agent and UI integration

Sentinel Risk Officer reports volatility, invalidation, sizing availability,
missing specifications, and event risk. Capitol exposes conditional strategy
scenarios alongside Phase 5.5 price-action evidence. The Master retains scenario
states and specialist disagreement while stating that no result is an approval
or instruction. The existing nine-agent roster remains unchanged; there was no
dedicated Strategy seat to replace.

The analysis API adds an optional `risk_strategy_intelligence` field. It is omitted
when no relevant evidence exists, preserving the prior empty-response shape. The
Market Analysis view displays all three scenarios, assumptions, quality factors,
conflicts, missing evidence, and limitations. The sidebar is unchanged.

## Remaining limitations

- No broker or MT5 contract/account data is accessed automatically.
- No verified currency-conversion or transaction-cost feed is integrated.
- No session-calendar specialist evidence is available.
- OHLCV price-action candidates do not establish institutional intent or resting
  orders.
- Strategy quality has not been statistically validated and is not a probability.
- No backtest, slippage model, portfolio optimizer, or execution path is included.
