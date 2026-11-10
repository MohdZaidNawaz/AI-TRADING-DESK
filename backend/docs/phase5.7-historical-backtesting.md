# Phase 5.7 — Historical Backtesting and Statistical Validation

## Scope

Phase 5.7 adds a deterministic, read-only historical validation boundary. It evaluates a documented subset of FRIDAY's existing technical, liquidity, and risk/strategy logic against already validated market-series evidence held by FRIDAY Core. It does not retrieve data, connect to a broker, place orders, authorize execution, tune a strategy, or predict future performance.

## Supported strategy

`trend_confirmed_scenario_v1` is the only supported strategy. It does not invent a selection rule that Phase 5.6 does not contain:

1. Phase 5.3 calculates the existing deterministic trend classification from the completed-candle prefix available at the decision time.
2. Phase 5.5 derives price-action liquidity candidates from that same prefix.
3. Phase 5.6 creates conditional bullish and bearish research scenarios.
4. A signal exists only when the Phase 5.3 trend is bullish or bearish and the matching Phase 5.6 scenario has an evidence-bound entry zone, stop, and target.
5. The signal is formed at a completed-candle close. It may enter only on the immediately following candle if that candle reaches the entry zone.

Phase 5.6 still selects no scenario in live research. The historical adapter above is a fixed validation rule, not a recommendation or automatic strategy.

## Simulation policy

- Only completed candles are eligible. Provisional candles are counted, reported, and excluded.
- Every decision receives a prefix ending at the current completed candle. Future candles cannot affect that decision.
- Missing intervals are reported and never filled. Duplicate timestamps or non-strict ordering fail closed.
- Only one position may be open. A pending entry expires after the next candle.
- When stop and target are both touched in one candle, the stop is applied first. This is a conservative response to unavailable intrabar ordering.
- A gap through an active stop or target exits at the observed opening price.
- A gap beyond the stop before an entry, followed by a possible entry-zone touch in the same candle, is not simulated because the intrabar sequence is unresolved.
- Maximum-holding exits use an observed candle close. A position still open at the end of the full sample or any validation window is not force-closed: it is excluded from completed-trade metrics and reported as incomplete.

## Costs and monetary results

Price-based gross returns are always calculated independently from validated OHLCV evidence. Net returns are produced only when all three cost components (spread, per-side slippage, and round-trip commission) are explicitly supplied with valid provenance. Explicit verified zero values are accepted; absent values are never inferred as zero. The report preserves the cost assessment, source, verification time, supplied values, missing components, and assumptions. Spread and slippage are applied to entry and exit prices. A non-zero verified commission can be applied only when an existing Phase 5.6 sizing result and account equity are available.

Account-currency P&L is produced only when the Phase 5.6 sizing context contains verified tick size, tick value, position size, and any required currency conversion. Missing contract or conversion facts remain unavailable. Gross-only reports are explicitly labelled and must not be described as net profitability.

## Metrics

The engine reports separate typed gross and net metric families for win/loss classification, profit/loss sums, profit factor, average win/loss, expectancy, compounded total return, maximum drawdown, streaks, and per-trade Sharpe. Net metrics are absent unless cost evidence is verified and applicable to every completed trade. Compatibility fields remain explicitly gross. Undefined denominators return an explicit `undefined` status. Sharpe requires at least 30 completed trades and non-zero return variance.

## Chronological validation

Development, out-of-sample, and optional walk-forward windows are each simulated independently within half-open bar-index boundaries. Earlier completed bars may supply indicator warmup, but a window cannot inherit a pre-window signal or position and cannot use a post-window bar to complete a trade. Open positions at a boundary are reported as incomplete rather than force-closed. The engine does not tune parameters in any section; window metrics are comparisons of the fixed documented rule. Small samples and incomplete coverage are warnings, not evidence of reliability.

## API and agents

`POST /api/backtests` accepts a typed instrument, timeframe, optional date range/source, fixed strategy parameters, and optional verified sizing/cost inputs. It reads only the current cached evidence snapshot. Multiple matching sources require an explicit `source_id`; the service never silently combines providers.

Oracle Quant, Sentinel Risk Officer, and the existing Master receive the resulting `BacktestReport`. Their output identifies it as historical validation evidence, distinguishes gross from verified net results, and preserves the literal-false authorization fields. No new agent seat was added.

## Known limitations

- The process-local market-series store is bounded to the existing data limit, so long historical tests require separately approved, validated evidence ingestion before the backtest request.
- Candle data cannot resolve intrabar event order.
- The fixed strategy may produce no trades when trend, structure, entry, stop, or target evidence is insufficient.
- The backtest does not model liquidity, partial fills, funding, swaps, taxes, or market impact unless a future reviewed contract provides verified inputs.
- A positive result is neither a forecast nor proof of statistical reliability, production readiness, or execution authorization.
