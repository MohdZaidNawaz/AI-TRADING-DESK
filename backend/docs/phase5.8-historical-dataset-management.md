# Phase 5.8 — Historical Dataset Management

Phase 5.8 adds an explicit, research-only path for retaining validated historical OHLCV evidence beyond the bounded process-local series store. It does not add broker connectivity, order submission, automated optimisation, or execution authority.

## Architecture

`HistoricalDatasetService` resolves an instrument through the existing catalogue/reference resolver and calls the existing asynchronous Twelve Data client and historical normalizer. Retrieval is user initiated. It is never started on application startup, analysis, or backtest submission.

The provider contract supports bounded `outputsize`, `start_date`, and `end_date` parameters but no opaque page token. Phase 5.8 therefore holds the requested end boundary constant and advances sequential `start_date` chunks only after a fully validated response. Each request is capped at 5,000 bars, an ingestion is capped at 10 requests and 50,000 validated bars, and a date range is capped at ten years. Defaults are three requests and 15,000 bars. There are no automatic retries. A rate limit blocks further requests for the life of that service instance because the provider does not expose a reliable reset time through this connector.

## Validation and coverage

Every page passes through `TwelveDataHistoricalConnector`, which enforces provider symbol and timeframe identity, timezone interpretation, finite OHLCV values, OHLC consistency, unique timestamps, chronological order, candle duration, source timestamps, retrieval timestamps, and completed/provisional state. Exact catalogue exchange mappings are sent to the provider and a conflicting returned venue fails closed; an omitted venue remains explicitly unverified. Page overlaps are deduplicated only when the normalized market observation is identical; conflicting overlap fails closed.

The dataset records observed timestamp discontinuities. Because FRIDAY has no authoritative trading-session calendar for every instrument and venue, `missing_interval_count` remains `null`: normal market closures are not asserted to be missing candles. No candle or price is manufactured. Coverage that does not span the requested interval, hits a request/bar bound, contains provisional candles, or ends after a provider failure is stored only as `partial`.

Twelve Data does not supply a reliable adjusted/unadjusted flag through the current response contract. The dataset records `adjustment_semantics=provider_unspecified`; it never infers adjustment behavior. Missing exchange metadata remains `venue=null` and `exchange_metadata_available=false`.

## Immutable storage

Datasets are stored below the ignored `backend/runtime/historical-datasets` directory as versioned JSON documents. Each document contains:

- schema and dataset versions;
- stable content-derived dataset ID and SHA-256 fingerprint;
- provider and canonical instrument identity;
- timeframe, venue availability, adjustment semantics, requested range and actual coverage;
- retrieval and creation timestamps;
- validated, completed and provisional counts;
- observed discontinuities, provider request count, completeness and bounded warning codes;
- the normalized immutable bars and original provider timestamp strings.

Writes use a new temporary file, flush and `fsync`, then publish it with an atomic same-directory hard link that fails if the immutable target already exists. Existing dataset IDs are revalidated and never silently overwritten. The store is protected by an in-process lock, defaults to 100 immutable datasets, and fails closed at the retention limit instead of deleting user evidence. Reads recompute the fingerprint and reject malformed, truncated, version-incompatible or mutated documents. Credentials, authorization headers and raw provider responses are never stored.

## API

- `POST /api/historical-datasets/ingestions` — explicit bounded ingestion.
- `GET /api/historical-datasets` — validated metadata list plus corrupt-document count.
- `GET /api/historical-datasets/{dataset_id}` — one validated metadata record.
- `POST /api/backtests` — optionally accepts `dataset_id` and `dataset_fingerprint`.

Dataset ingestion reports `available`, `partial`, `unavailable`, `not_configured`, `rate_limited`, `timeout`, `invalid_response`, or `storage_error`. Literal production authorization, submission and execution fields remain false.

Backtesting remains read-only and cannot call a provider. When an exact dataset is selected, its file fingerprint, instrument, timeframe, requested date range and completeness are checked before conversion to the existing `MarketSeriesEvidence` contract. Partial or provisional datasets are rejected by the backtesting policy. Omitting `dataset_id` preserves Phase 5.7's existing bounded-cache behavior.

## Frontend

The existing Strategy Validation panel can explicitly request a dataset, inspect actual coverage/counts/source/venue/warnings, select its exact ID, and submit that ID plus fingerprint to the backtest endpoint. The only progress state shown is the active HTTP request; no background polling or fabricated progress is used. A successful empty retrieval, provider error, rate limit, partial dataset and complete dataset remain distinct.

## Security and limitations

- No live provider request is made by tests or by backtesting.
- No provider fallback, mixing, retry loop, background polling, broker, MT5, account access, order submission or execution is present.
- Dataset membership is evidence availability, not proof of market quality, profitability or execution readiness.
- The local JSON store protects atomicity and detects mutation, but it is not independent rollback protection and is not a substitute for external backup or authenticated storage.
- Process-local locking does not coordinate independent backend processes; deployments requiring multiple writers need an externally coordinated durable store.
- Provider retention, plan limits and historical depth remain external constraints.
