"""Thread-safe bounded storage for provider-neutral historical market series."""

from __future__ import annotations

from threading import RLock

from app.evidence.models import (
    MarketSeriesEvidence,
    MarketSeriesSnapshot,
    canonical_symbol,
    symbols_match,
)
from app.models.market_series import MarketBarState, MarketSeriesKey, NormalizedMarketBar


class MarketSeriesStore:
    """Keep bounded bar series separate from point-in-time market events.

    Series capacity uses stable series IDs and bar capacity retains the latest
    timestamps. These policies make the retained result independent of arrival
    order for the same input set.
    """

    def __init__(self, *, max_series: int = 20, max_bars_per_series: int = 500) -> None:
        _validate_limit(max_series, "max_series")
        _validate_limit(max_bars_per_series, "max_bars_per_series")
        self._max_series = max_series
        self._max_bars_per_series = max_bars_per_series
        self._series: dict[MarketSeriesKey, dict[str, NormalizedMarketBar]] = {}
        self._lock = RLock()

    def ingest(self, bar: NormalizedMarketBar) -> MarketSeriesEvidence | None:
        """Insert or deterministically revise one semantic bar.

        ``None`` means the series was outside the configured deterministic
        series bound. No provider call, synthesis, or agent run occurs here.
        """
        if not isinstance(bar, NormalizedMarketBar):
            raise TypeError("bar must be a NormalizedMarketBar")

        key = bar.series_key
        with self._lock:
            retained_keys = sorted(
                {*self._series.keys(), key},
                key=lambda item: item.series_id,
            )[: self._max_series]
            if key not in retained_keys:
                return None
            for existing_key in tuple(self._series):
                if existing_key not in retained_keys:
                    del self._series[existing_key]

            bars = self._series.setdefault(key, {})
            self._validate_series_metadata(bars, bar)
            existing = bars.get(bar.bar_id)
            if existing is None or _revision_rank(bar) > _revision_rank(existing):
                bars[bar.bar_id] = bar
            self._trim_bars(bars)
            if bar.bar_id not in bars:
                return None
            return MarketSeriesEvidence.from_bars(key, tuple(bars.values()))

    def snapshot(self, *, symbol: str | None = None) -> MarketSeriesSnapshot:
        requested_symbol = canonical_symbol(symbol)
        with self._lock:
            evidence = tuple(
                MarketSeriesEvidence.from_bars(key, tuple(bars.values()))
                for key, bars in self._series.items()
                if requested_symbol is None or symbols_match(key.symbol, requested_symbol)
            )
        return MarketSeriesSnapshot(
            series=tuple(sorted(evidence, key=_series_sort_key)),
        )

    def latest_completed(
        self,
        key: MarketSeriesKey,
        *,
        limit: int | None = None,
    ) -> tuple[NormalizedMarketBar, ...]:
        if limit is not None:
            _validate_limit(limit, "limit")
        with self._lock:
            bars = tuple(self._series.get(key, {}).values())
        completed = tuple(
            sorted(
                (bar for bar in bars if bar.state is MarketBarState.COMPLETED),
                key=lambda item: (item.opened_at, item.bar_id),
            )
        )
        return completed[-limit:] if limit is not None else completed

    def get_series(self, key: MarketSeriesKey) -> MarketSeriesEvidence | None:
        """Return one immutable series snapshot by its exact provider-neutral key."""
        if not isinstance(key, MarketSeriesKey):
            raise TypeError("key must be a MarketSeriesKey")
        with self._lock:
            bars = tuple(self._series.get(key, {}).values())
        return MarketSeriesEvidence.from_bars(key, bars) if bars else None

    @property
    def series_count(self) -> int:
        with self._lock:
            return len(self._series)

    @property
    def bar_count(self) -> int:
        with self._lock:
            return sum(len(bars) for bars in self._series.values())

    def _trim_bars(self, bars: dict[str, NormalizedMarketBar]) -> None:
        retained = set(
            bar.bar_id
            for bar in sorted(
                bars.values(),
                key=lambda item: (item.opened_at, item.bar_id),
                reverse=True,
            )[: self._max_bars_per_series]
        )
        for bar_id in tuple(bars):
            if bar_id not in retained:
                del bars[bar_id]

    @staticmethod
    def _validate_series_metadata(
        bars: dict[str, NormalizedMarketBar],
        candidate: NormalizedMarketBar,
    ) -> None:
        if not bars:
            return
        existing = next(iter(bars.values()))
        if existing.asset_class is not candidate.asset_class:
            raise ValueError("a market series cannot mix asset classes")
        if existing.source != candidate.source:
            raise ValueError("a market series cannot mix source metadata")


def _revision_rank(bar: NormalizedMarketBar) -> tuple[int, int, float, str]:
    return (
        int(bar.revision is not None),
        bar.revision if bar.revision is not None else -1,
        bar.received_at.timestamp(),
        bar.content_fingerprint(),
    )


def _series_sort_key(evidence: MarketSeriesEvidence) -> tuple[str, str, str, str]:
    return (
        evidence.symbol,
        evidence.venue or "",
        evidence.timeframe,
        evidence.series_id,
    )


def _validate_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
