"""Bounded, process-local storage for immutable FRIDAY evidence projections."""

from __future__ import annotations

from threading import RLock

from app.evidence.models import (
    CorporateActionEvidence,
    EconomicEventEvidence,
    EstimateSeriesEvidence,
    EvidenceSnapshot,
    FundamentalSeriesEvidence,
    IssuerReferenceEvidence,
    MarketEvidence,
    MarketSeriesEvidence,
    MacroSeriesEvidence,
    NewsEvidence,
    PositioningSeriesEvidence,
    canonical_symbol,
    symbols_match,
)
from app.evidence.corporate_action_store import CorporateActionStore
from app.evidence.issuer_reference_store import IssuerReferenceStore
from app.evidence.economic_store import EconomicEventStore
from app.evidence.fundamental_store import FundamentalStore
from app.evidence.estimate_store import EstimateStore
from app.evidence.macro_store import MacroSeriesStore
from app.evidence.positioning_store import PositioningStore
from app.evidence.series_store import MarketSeriesStore
from app.models.events import NormalizedDataEvent, NormalizedNewsResearchEvent
from app.models.market_series import NormalizedMarketBar
from app.models.economic_events import NormalizedEconomicEvent
from app.models.macro_series import NormalizedMacroObservation
from app.models.positioning import NormalizedPositioningObservation
from app.models.fundamentals import NormalizedFundamentalObservation
from app.models.estimates import NormalizedEstimateObservation
from app.models.corporate_actions import NormalizedCorporateAction
from app.models.issuer_reference import NormalizedIssuerInstrumentReference


class EvidenceStore:
    """Thread-safe, bounded evidence store with deterministic snapshots.

    The store intentionally has no persistence lifecycle: it belongs to one
    application process and is empty after that process restarts.
    """

    def __init__(
        self,
        *,
        max_market_events: int = 100,
        max_news_events: int = 100,
        max_economic_events: int = 500,
        max_market_series: int = 20,
        max_bars_per_series: int = 500,
        max_macro_series: int = 20,
        max_macro_occurrences_per_series: int = 500,
        max_macro_vintages_per_occurrence: int = 5,
        max_positioning_series: int = 20,
        max_positioning_occurrences_per_series: int = 500,
        max_positioning_vintages_per_occurrence: int = 5,
        max_fundamental_series: int = 20,
        max_fundamental_occurrences_per_series: int = 500,
        max_fundamental_vintages_per_occurrence: int = 5,
        max_estimate_series: int = 20,
        max_estimate_occurrences_per_series: int = 500,
        max_estimate_vintages_per_occurrence: int = 5,
        max_corporate_action_instruments: int = 20,
        max_corporate_action_occurrences_per_instrument: int = 500,
        max_corporate_action_versions_per_occurrence: int = 5,
        max_issuer_reference_instruments: int = 100,
        max_issuer_reference_profiles_per_instrument: int = 5,
        max_issuer_reference_versions_per_profile: int = 5,
        market_series_store: MarketSeriesStore | None = None,
        economic_event_store: EconomicEventStore | None = None,
        macro_series_store: MacroSeriesStore | None = None,
        positioning_store: PositioningStore | None = None,
        fundamental_store: FundamentalStore | None = None,
        estimate_store: EstimateStore | None = None,
        corporate_action_store: CorporateActionStore | None = None,
        issuer_reference_store: IssuerReferenceStore | None = None,
    ) -> None:
        _validate_limit(max_market_events, "max_market_events")
        _validate_limit(max_news_events, "max_news_events")
        _validate_limit(max_economic_events, "max_economic_events")
        _validate_limit(max_market_series, "max_market_series")
        _validate_limit(max_bars_per_series, "max_bars_per_series")
        _validate_limit(max_macro_series, "max_macro_series")
        _validate_limit(max_macro_occurrences_per_series, "max_macro_occurrences_per_series")
        _validate_limit(max_macro_vintages_per_occurrence, "max_macro_vintages_per_occurrence")
        _validate_limit(max_positioning_series, "max_positioning_series")
        _validate_limit(
            max_positioning_occurrences_per_series,
            "max_positioning_occurrences_per_series",
        )
        _validate_limit(
            max_positioning_vintages_per_occurrence,
            "max_positioning_vintages_per_occurrence",
        )
        _validate_limit(max_fundamental_series, "max_fundamental_series")
        _validate_limit(
            max_fundamental_occurrences_per_series,
            "max_fundamental_occurrences_per_series",
        )
        _validate_limit(
            max_fundamental_vintages_per_occurrence,
            "max_fundamental_vintages_per_occurrence",
        )
        _validate_limit(max_estimate_series, "max_estimate_series")
        _validate_limit(
            max_estimate_occurrences_per_series,
            "max_estimate_occurrences_per_series",
        )
        _validate_limit(
            max_estimate_vintages_per_occurrence,
            "max_estimate_vintages_per_occurrence",
        )
        _validate_limit(max_corporate_action_instruments, "max_corporate_action_instruments")
        _validate_limit(
            max_corporate_action_occurrences_per_instrument,
            "max_corporate_action_occurrences_per_instrument",
        )
        _validate_limit(
            max_corporate_action_versions_per_occurrence,
            "max_corporate_action_versions_per_occurrence",
        )
        _validate_limit(max_issuer_reference_instruments, "max_issuer_reference_instruments")
        _validate_limit(
            max_issuer_reference_profiles_per_instrument,
            "max_issuer_reference_profiles_per_instrument",
        )
        _validate_limit(
            max_issuer_reference_versions_per_profile,
            "max_issuer_reference_versions_per_profile",
        )
        self._max_market_events = max_market_events
        self._max_news_events = max_news_events
        self._market_events: dict[str, MarketEvidence] = {}
        self._news_events: dict[str, NewsEvidence] = {}
        self._economic_event_store = economic_event_store or EconomicEventStore(
            max_events=max_economic_events
        )
        self._market_series_store = market_series_store or MarketSeriesStore(
            max_series=max_market_series,
            max_bars_per_series=max_bars_per_series,
        )
        self._macro_series_store = macro_series_store or MacroSeriesStore(
            max_series=max_macro_series,
            max_occurrences_per_series=max_macro_occurrences_per_series,
            max_vintages_per_occurrence=max_macro_vintages_per_occurrence,
        )
        self._positioning_store = positioning_store or PositioningStore(
            max_series=max_positioning_series,
            max_occurrences_per_series=max_positioning_occurrences_per_series,
            max_vintages_per_occurrence=max_positioning_vintages_per_occurrence,
        )
        self._fundamental_store = fundamental_store or FundamentalStore(
            max_series=max_fundamental_series,
            max_occurrences_per_series=max_fundamental_occurrences_per_series,
            max_vintages_per_occurrence=max_fundamental_vintages_per_occurrence,
        )
        self._estimate_store = estimate_store or EstimateStore(
            max_series=max_estimate_series,
            max_occurrences_per_series=max_estimate_occurrences_per_series,
            max_vintages_per_occurrence=max_estimate_vintages_per_occurrence,
        )
        self._corporate_action_store = corporate_action_store or CorporateActionStore(
            max_instruments=max_corporate_action_instruments,
            max_occurrences_per_instrument=max_corporate_action_occurrences_per_instrument,
            max_versions_per_occurrence=max_corporate_action_versions_per_occurrence,
        )
        self._issuer_reference_store = issuer_reference_store or IssuerReferenceStore(
            max_instruments=max_issuer_reference_instruments,
            max_profiles_per_instrument=max_issuer_reference_profiles_per_instrument,
            max_versions_per_profile=max_issuer_reference_versions_per_profile,
        )
        self._lock = RLock()

    def ingest_market_event(self, event: NormalizedDataEvent) -> MarketEvidence:
        """Project and retain one normalized market event."""
        evidence = MarketEvidence.from_event(event)
        with self._lock:
            existing = self._market_events.get(evidence.provenance.event_id)
            if existing is None or _market_sort_key(evidence) < _market_sort_key(existing):
                self._market_events[evidence.provenance.event_id] = evidence
            self._trim(self._market_events, self._max_market_events, _market_sort_key)
            return self._market_events.get(evidence.provenance.event_id, evidence)

    def ingest_news_event(
        self,
        event: NormalizedNewsResearchEvent,
    ) -> NewsEvidence:
        """Project and retain one normalized news/research event."""
        evidence = NewsEvidence.from_event(event)
        with self._lock:
            existing = self._news_events.get(evidence.provenance.event_id)
            if existing is None or _news_sort_key(evidence) < _news_sort_key(existing):
                self._news_events[evidence.provenance.event_id] = evidence
            self._trim(self._news_events, self._max_news_events, _news_sort_key)
            return self._news_events.get(evidence.provenance.event_id, evidence)

    def ingest_market_bar(
        self,
        bar: NormalizedMarketBar,
    ) -> MarketSeriesEvidence | None:
        """Retain one bar in the separate bounded historical-series store."""
        with self._lock:
            return self._market_series_store.ingest(bar)

    def ingest_economic_event(
        self,
        event: NormalizedEconomicEvent,
    ) -> EconomicEventEvidence:
        """Retain one version in the separate economic-event store."""
        with self._lock:
            return self._economic_event_store.ingest(event)

    def ingest_macro_observation(
        self,
        observation: NormalizedMacroObservation,
    ) -> MacroSeriesEvidence | None:
        """Retain one macro observation in its dedicated bounded store."""
        with self._lock:
            return self._macro_series_store.ingest(observation)

    def ingest_positioning_observation(
        self,
        observation: NormalizedPositioningObservation,
    ) -> PositioningSeriesEvidence | None:
        """Retain one reported positioning observation in its dedicated store."""
        with self._lock:
            return self._positioning_store.ingest(observation)

    def ingest_fundamental_observation(
        self,
        observation: NormalizedFundamentalObservation,
    ) -> FundamentalSeriesEvidence | None:
        """Retain one financial-statement observation in its dedicated store."""
        with self._lock:
            return self._fundamental_store.ingest(observation)

    def ingest_estimate_observation(
        self,
        observation: NormalizedEstimateObservation,
    ) -> EstimateSeriesEvidence | None:
        """Retain one source-reported estimate in its dedicated bounded store."""
        with self._lock:
            return self._estimate_store.ingest(observation)

    def ingest_corporate_action(
        self,
        action: NormalizedCorporateAction,
    ) -> CorporateActionEvidence | None:
        """Retain one source-reported action in its dedicated bounded store."""
        with self._lock:
            return self._corporate_action_store.ingest(action)

    def ingest_issuer_reference(
        self,
        profile: NormalizedIssuerInstrumentReference,
    ) -> IssuerReferenceEvidence | None:
        """Retain one source-reported issuer/listing reference profile."""
        with self._lock:
            return self._issuer_reference_store.ingest(profile)

    def snapshot(self, *, symbol: str | None = None) -> EvidenceSnapshot:
        """Return a symbol-scoped, immutable, deterministically ordered snapshot."""
        requested_symbol = canonical_symbol(symbol)
        with self._lock:
            market_evidence = tuple(
                sorted(
                    (
                        evidence
                        for evidence in self._market_events.values()
                        if _market_matches_symbol(evidence, requested_symbol)
                    ),
                    key=_market_sort_key,
                )
            )
            news_evidence = tuple(
                sorted(
                    (
                        evidence
                        for evidence in self._news_events.values()
                        if _news_matches_symbol(evidence, requested_symbol)
                    ),
                    key=_news_sort_key,
                )
            )
            market_series = self._market_series_store.snapshot(symbol=requested_symbol).series
            economic_events = self._economic_event_store.snapshot().events
            macro_series = self._macro_series_store.snapshot().series
            positioning_series = self._positioning_store.snapshot(symbol=requested_symbol).series
            fundamental_series = self._fundamental_store.snapshot(symbol=requested_symbol).series
            estimate_series = self._estimate_store.snapshot(symbol=requested_symbol).series
            corporate_actions = self._corporate_action_store.snapshot(symbol=requested_symbol).actions
            issuer_references = self._issuer_reference_store.snapshot(symbol=requested_symbol).profiles

        return EvidenceSnapshot(
            market_evidence=market_evidence,
            news_evidence=news_evidence,
            market_series=market_series,
            economic_events=economic_events,
            macro_series=macro_series,
            positioning_series=positioning_series,
            fundamental_series=fundamental_series,
            estimate_series=estimate_series,
            corporate_actions=corporate_actions,
            issuer_references=issuer_references,
            symbol=requested_symbol,
        )

    @property
    def market_count(self) -> int:
        with self._lock:
            return len(self._market_events)

    @property
    def news_count(self) -> int:
        with self._lock:
            return len(self._news_events)

    @property
    def market_series_count(self) -> int:
        with self._lock:
            return self._market_series_store.series_count

    @property
    def market_bar_count(self) -> int:
        with self._lock:
            return self._market_series_store.bar_count

    @property
    def market_series_store(self) -> MarketSeriesStore:
        return self._market_series_store

    @property
    def economic_count(self) -> int:
        with self._lock:
            return self._economic_event_store.event_count

    @property
    def economic_event_store(self) -> EconomicEventStore:
        return self._economic_event_store

    @property
    def macro_series_count(self) -> int:
        with self._lock:
            return self._macro_series_store.series_count

    @property
    def macro_observation_count(self) -> int:
        with self._lock:
            return self._macro_series_store.observation_count

    @property
    def macro_series_store(self) -> MacroSeriesStore:
        return self._macro_series_store

    @property
    def positioning_series_count(self) -> int:
        with self._lock:
            return self._positioning_store.series_count

    @property
    def positioning_observation_count(self) -> int:
        with self._lock:
            return self._positioning_store.observation_count

    @property
    def positioning_store(self) -> PositioningStore:
        return self._positioning_store

    @property
    def fundamental_series_count(self) -> int:
        with self._lock:
            return self._fundamental_store.series_count

    @property
    def fundamental_observation_count(self) -> int:
        with self._lock:
            return self._fundamental_store.observation_count

    @property
    def fundamental_store(self) -> FundamentalStore:
        return self._fundamental_store

    @property
    def estimate_series_count(self) -> int:
        with self._lock:
            return self._estimate_store.series_count

    @property
    def estimate_observation_count(self) -> int:
        with self._lock:
            return self._estimate_store.observation_count

    @property
    def estimate_store(self) -> EstimateStore:
        return self._estimate_store

    @property
    def corporate_action_count(self) -> int:
        with self._lock:
            return self._corporate_action_store.action_count

    @property
    def corporate_action_store(self) -> CorporateActionStore:
        return self._corporate_action_store

    @property
    def issuer_reference_count(self) -> int:
        with self._lock:
            return self._issuer_reference_store.profile_count

    @property
    def issuer_reference_store(self) -> IssuerReferenceStore:
        return self._issuer_reference_store

    @staticmethod
    def _trim(
        events: dict[str, MarketEvidence] | dict[str, NewsEvidence],
        limit: int,
        sort_key: object,
    ) -> None:
        while len(events) > limit:
            oldest_event_id = max(
                events,
                key=lambda event_id: sort_key(events[event_id]),  # type: ignore[operator]
            )
            del events[oldest_event_id]


def _validate_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _market_matches_symbol(
    evidence: MarketEvidence,
    requested_symbol: str | None,
) -> bool:
    return requested_symbol is None or symbols_match(evidence.symbol, requested_symbol)


def _news_matches_symbol(
    evidence: NewsEvidence,
    requested_symbol: str | None,
) -> bool:
    return requested_symbol is None or any(
        symbols_match(symbol, requested_symbol) for symbol in evidence.symbols
    )


def _market_sort_key(
    evidence: MarketEvidence,
) -> tuple[float, str, str, str, str, str]:
    return (
        -evidence.timestamp.timestamp(),
        evidence.provenance.event_id,
        evidence.provenance.source_id,
        evidence.symbol or "",
        evidence.event,
        repr(evidence),
    )


def _news_sort_key(
    evidence: NewsEvidence,
) -> tuple[float, str, str, str, str, str]:
    return (
        -evidence.published_at.timestamp(),
        evidence.provenance.event_id,
        evidence.provenance.source_id,
        evidence.url,
        evidence.title,
        repr(evidence),
    )
