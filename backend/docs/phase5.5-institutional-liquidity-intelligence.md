# Phase 5.5 — Institutional Liquidity Intelligence

## Scope and safety boundary

Phase 5.5 adds deterministic OHLCV price-action research. It does not observe
resting orders, bank positions, institutional intent, exchange order books, or
broker flow. “Liquidity level” and “order block” always mean a reproducible
price-action candidate. The report structurally fixes production authorization,
submission, and execution authority to `false`.

Only validated `completed` bars whose close and receipt timestamps are at or
before `evaluated_at` participate. Provisional, future-closing, and late-received
bars are excluded. Calculations use only the latest contiguous segment when a
series has missing intervals.

## Methods

### Structure and pivots

A confirmed pivot is a strict high or low against `swing_window` completed bars
on both sides. The default window is two. Trailing candidates without the right
confirmation window are returned separately as `provisional`; they cannot drive
confirmed liquidity, break, or order-block output.

Successive confirmed highs are HH, LH, or equal high; lows are HL, LL, or equal
low. Equality uses a configurable absolute tolerance derived from price:

`tolerance = max(abs(price) × tolerance_bps / 10,000, 1e-12)`

The default is 5 basis points. A new extreme beyond every earlier same-kind pivot
is external structure; other pivots are internal. The latest HH+HL is bullish,
LH+LL bearish, and other sufficiently populated combinations are range.

### Liquidity candidates and sweeps

Confirmed highs form buy-side liquidity candidates; confirmed lows form
sell-side candidates. Pivots inside the equality tolerance are clustered. A
buy-side sweep requires price to trade above the level and close back below it;
a sell-side sweep is the inverse. Penetration followed by a close beyond the
level is retained as `wick_only` penetration and invalidates that candidate—it
is not called a confirmed sweep.

### Fair Value Gaps

The three-candle method compares candle one with candle three:

- bullish: candle-three low is above candle-one high;
- bearish: candle-three high is below candle-one low.

The default minimum gap is 1 basis point. Later overlap marks partial fill;
reaching the opposite boundary marks full fill and invalidation. The formation
bars and the relevant fill bar are retained as OHLC evidence.

### BOS and CHoCH

A break references a pivot already confirmed before the break candle opened.
Close beyond the pivot is `confirmed`; wick penetration without the close is
`wick_only`. A break against the preceding bullish/bearish structure is CHoCH;
other structure/range breaks are BOS candidates. Later close beyond the opposite
reference level marks invalidation.

### Order-block candidates

Only a close-confirmed BOS/CHoCH can create an order-block candidate. The source
is the last opposing candle before the break: bearish before bullish, bullish
before bearish. Its high/low are the boundaries. Later overlap is mitigation;
close beyond the distal boundary is invalidation. This method does not establish
that an institution placed an order there.

### Multi-timeframe alignment and freshness

Supported timeframes are 1m, 5m, 15m, 30m, 1h, 4h, 1d, and 1w. Only validated,
fresh series are compared; missing timeframes remain explicit. Alignment is
returned only with at least two usable series. Freshness expires after the larger
of 24 hours or two timeframe durations. Stale results remain historical context
and are not presented as current market state.

## Integration

The existing Capitol specialist receives the report and exposes counts,
provenance, freshness, alignment, missing inputs, and limitations. The Master
adds these observations to research synthesis while explicitly denying that they
prove institutional activity or create a trade signal. The Market Analysis view
shows levels, sweeps, FVGs, breaks, order-block candidates, confirmation states,
provider timestamps, and limitations without adding a chart or changing the
sidebar.
