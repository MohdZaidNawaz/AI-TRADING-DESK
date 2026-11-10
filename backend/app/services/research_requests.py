"""Deterministic instrument and natural-language research-request resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Iterable

from app.evidence.models import EvidenceSnapshot
from app.models.instruments import (
    ExchangeResolution,
    InstrumentCatalogueEntry,
    InstrumentCategory,
)
from app.models.research_requests import (
    EvidenceAvailability,
    EvidenceAvailabilityStatus,
    HistoricalRetrievalDiagnostic,
    HistoricalRetrievalStatus,
    InstrumentIdentitySource,
    InstrumentResolutionStatus,
    ResolvedResearchInstrument,
    ResearchEvidenceAvailability,
    ResearchIntent,
    ResearchLookback,
    ResearchRequestContext,
)
from app.models.schemas import AnalyzeRequest


_TIMEFRAME_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?<![a-z0-9])1\s*[- ]?minute(?:s)?(?![a-z0-9])|(?<![a-z0-9])1m(?![a-z0-9])", "1m"),
    (r"(?<![a-z0-9])5\s*[- ]?minute(?:s)?(?![a-z0-9])|(?<![a-z0-9])5m(?![a-z0-9])", "5m"),
    (r"(?<![a-z0-9])15\s*[- ]?minute(?:s)?(?![a-z0-9])|(?<![a-z0-9])15m(?![a-z0-9])", "15m"),
    (r"(?<![a-z0-9])30\s*[- ]?minute(?:s)?(?![a-z0-9])|(?<![a-z0-9])30m(?![a-z0-9])", "30m"),
    (r"(?<![a-z0-9])1\s*[- ]?hour(?:s)?(?![a-z0-9])|(?<![a-z0-9])1h(?![a-z0-9])", "1h"),
    (r"(?<![a-z0-9])4\s*[- ]?hour(?:s)?(?![a-z0-9])|(?<![a-z0-9])4h(?![a-z0-9])", "4h"),
    (r"(?<![a-z0-9])1\s*[- ]?day(?![a-z0-9])|(?<![a-z0-9])1d(?![a-z0-9])|\bdaily\b", "1d"),
    (r"(?<![a-z0-9])1\s*[- ]?week(?![a-z0-9])|(?<![a-z0-9])1w(?![a-z0-9])|\bweekly\b", "1w"),
)
_TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1_800,
    "1h": 3_600,
    "4h": 14_400,
    "1d": 86_400,
    "1w": 604_800,
}
_LOOKBACK = re.compile(
    r"\b(?:last|past|lookback(?:\s+of)?)\s+(\d{1,6})\s*"
    r"(bars?|minutes?|hours?|days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_COMPARISON = re.compile(r"\b(compare|versus|vs\.?|against)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _ReferenceSubject:
    canonical_symbol: str
    category: InstrumentCategory
    display_name: str
    base_currency: str
    quote_currency: str
    aliases: tuple[str, ...]


_REFERENCE_SUBJECTS: tuple[_ReferenceSubject, ...] = (
    _ReferenceSubject("XAU/USD", InstrumentCategory.COMMODITY, "Gold / US Dollar", "XAU", "USD", ("XAUUSD", "XAU/USD", "Gold", "Spot Gold")),
    _ReferenceSubject("XAG/USD", InstrumentCategory.COMMODITY, "Silver / US Dollar", "XAG", "USD", ("XAGUSD", "XAG/USD", "Silver", "Spot Silver")),
    _ReferenceSubject("EUR/USD", InstrumentCategory.FOREX, "Euro / US Dollar", "EUR", "USD", ("EURUSD", "EUR/USD")),
    _ReferenceSubject("GBP/USD", InstrumentCategory.FOREX, "British Pound / US Dollar", "GBP", "USD", ("GBPUSD", "GBP/USD")),
    _ReferenceSubject("GBP/JPY", InstrumentCategory.FOREX, "British Pound / Japanese Yen", "GBP", "JPY", ("GBPJPY", "GBP/JPY")),
    _ReferenceSubject("BTC/USD", InstrumentCategory.CRYPTO, "Bitcoin / US Dollar", "BTC", "USD", ("BTCUSD", "BTC/USD", "Bitcoin")),
    _ReferenceSubject("ETH/USD", InstrumentCategory.CRYPTO, "Ethereum / US Dollar", "ETH", "USD", ("ETHUSD", "ETH/USD", "Ethereum", "Ether")),
)
_AMBIGUOUS_ALIASES = {"USOIL": "CONTRACT_OR_PROVIDER_IDENTITY_REQUIRED"}


class ResearchRequestService:
    """Resolve research subjects without fetching data or creating catalogue rows."""

    def __init__(
        self,
        *,
        catalogue_entries: Iterable[InstrumentCatalogueEntry] = (),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._catalogue_entries = tuple(catalogue_entries)
        self._now = now or (lambda: datetime.now(UTC))

    def prepare(self, request: AnalyzeRequest) -> ResearchRequestContext:
        expressions = self._extract_instrument_expressions(request)
        primary_expression = request.symbol or (
            expressions[0] if expressions else self._fallback_expression(request.query)
        )
        primary = self.resolve_instrument(primary_expression)

        comparisons: list[ResolvedResearchInstrument] = []
        if _COMPARISON.search(request.query):
            for expression in expressions:
                if _normalized_match_key(expression) == _normalized_match_key(primary_expression):
                    continue
                resolved = self.resolve_instrument(expression)
                if all(
                    _resolution_identity(existing) != _resolution_identity(resolved)
                    for existing in comparisons
                ):
                    comparisons.append(resolved)

        intents = self._intents(request.query, has_comparison=bool(comparisons))
        timeframe = self._timeframe(request.query)
        missing: list[str] = []
        if primary.status is InstrumentResolutionStatus.UNRESOLVED:
            missing.append("instrument_unresolved")
        elif primary.status is InstrumentResolutionStatus.AMBIGUOUS:
            missing.append("instrument_identity_ambiguous")
        if timeframe is None:
            missing.append("timeframe_unspecified")
        if ResearchIntent.COMPARISON in intents and not comparisons:
            missing.append("comparison_instrument_missing")

        return ResearchRequestContext(
            primary_instrument=primary,
            comparison_instruments=tuple(comparisons),
            timeframe=timeframe,  # type: ignore[arg-type]
            lookback=self._lookback(request.query),
            intents=intents,
            unavailable_capabilities=self._unavailable_capabilities(intents),
            missing_information=tuple(missing),
            evidence=self._empty_availability(primary),
        )

    def assess_evidence(
        self,
        context: ResearchRequestContext,
        snapshot: EvidenceSnapshot,
        *,
        derived_feature_count: int,
        historical_retrieval: HistoricalRetrievalDiagnostic | None = None,
    ) -> ResearchRequestContext:
        if context.primary_instrument.status is not InstrumentResolutionStatus.RESOLVED:
            unavailable = _availability(
                EvidenceAvailabilityStatus.UNAVAILABLE,
                "INSTRUMENT_IDENTITY_NOT_RESOLVED",
            )
            return context.model_copy(
                update={
                    "evidence": ResearchEvidenceAvailability(
                        instrument_identity=context.evidence.instrument_identity,
                        current_quote=unavailable,
                        historical_ohlcv=unavailable,
                        derived_features=unavailable,
                        news=unavailable,
                        macro=unavailable,
                        positioning_order_flow=unavailable,
                    )
                }
            )

        now = self._now().astimezone(UTC)
        historical = self._historical_availability(
            context,
            snapshot,
            now,
            historical_retrieval=historical_retrieval,
        )
        derived = (
            _availability(
                EvidenceAvailabilityStatus.AVAILABLE,
                "DERIVED_FEATURES_PRESENT",
                derived_feature_count,
            )
            if derived_feature_count
            else _availability(
                EvidenceAvailabilityStatus.UNKNOWN,
                "DERIVED_FEATURES_NOT_PRESENT",
            )
        )
        return context.model_copy(
            update={
                "evidence": ResearchEvidenceAvailability(
                    instrument_identity=context.evidence.instrument_identity,
                    current_quote=self._quote_availability(snapshot, now),
                    historical_ohlcv=historical,
                    derived_features=derived,
                    news=_presence(snapshot.news_evidence, "NEWS_EVIDENCE"),
                    macro=_presence(
                        (*snapshot.economic_events, *snapshot.macro_series),
                        "MACRO_EVIDENCE",
                    ),
                    positioning_order_flow=_presence(
                        snapshot.positioning_series,
                        "POSITIONING_EVIDENCE",
                    ),
                )
            }
        )

    def resolve_instrument(self, expression: str) -> ResolvedResearchInstrument:
        cleaned = expression.strip()
        if not cleaned:
            return _unresolved(expression or "unknown", "INSTRUMENT_EXPRESSION_MISSING")
        key = _normalized_match_key(cleaned)
        direct = self._catalogue_matches(cleaned)
        reference = _reference_for_key(key)
        if reference is not None:
            canonical_matches = tuple(
                item
                for item in self._catalogue_entries
                if _normalized_match_key(item.canonical_symbol)
                == _normalized_match_key(reference.canonical_symbol)
            )
            direct = _deduplicate_entries((*direct, *canonical_matches))

        if len(direct) > 1:
            return _ambiguous(cleaned, direct, "MULTIPLE_CATALOGUE_IDENTITIES")
        if len(direct) == 1:
            entry = direct[0]
            exact_identity = key in {
                _normalized_match_key(entry.instrument_id),
                _normalized_match_key(entry.canonical_instrument_id),
            }
            if (
                entry.exchange_resolution is ExchangeResolution.LEGACY_UNRESOLVED
                and not exact_identity
            ):
                return _ambiguous(cleaned, direct, "LEGACY_EXCHANGE_UNRESOLVED")
            return _from_catalogue(cleaned, entry)
        if key in _AMBIGUOUS_ALIASES:
            return ResolvedResearchInstrument(
                expression=cleaned,
                status=InstrumentResolutionStatus.AMBIGUOUS,
                identity_source=InstrumentIdentitySource.NONE,
                reason_codes=(_AMBIGUOUS_ALIASES[key],),
            )
        if reference is not None:
            return ResolvedResearchInstrument(
                expression=cleaned,
                status=InstrumentResolutionStatus.RESOLVED,
                identity_source=InstrumentIdentitySource.STANDARD_REFERENCE,
                canonical_symbol=reference.canonical_symbol,
                category=reference.category,
                display_name=reference.display_name,
                base_currency=reference.base_currency,
                quote_currency=reference.quote_currency,
                reason_codes=(
                    "STANDARD_RESEARCH_SUBJECT_RESOLVED",
                    "CATALOGUE_IDENTITY_NOT_PRESENT",
                ),
            )
        return _unresolved(cleaned, "INSTRUMENT_NOT_FOUND")

    def _catalogue_matches(self, expression: str) -> tuple[InstrumentCatalogueEntry, ...]:
        key = _normalized_match_key(expression)
        return _deduplicate_entries(
            item for item in self._catalogue_entries if key in _entry_match_keys(item)
        )

    def _extract_instrument_expressions(self, request: AnalyzeRequest) -> tuple[str, ...]:
        query = request.query
        comparison_expressions = _comparison_expressions(query)
        if comparison_expressions:
            return comparison_expressions
        candidates: list[tuple[int, int, str]] = []
        aliases = {
            alias for subject in _REFERENCE_SUBJECTS for alias in subject.aliases
        } | set(_AMBIGUOUS_ALIASES)
        aliases.update(
            value
            for entry in self._catalogue_entries
            for value in _entry_search_values(entry)
            if len(value) >= 2
        )
        for alias in sorted(aliases, key=len, reverse=True):
            pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            for match in pattern.finditer(query):
                if any(
                    match.start() < end and match.end() > start
                    for start, end, _ in candidates
                ):
                    continue
                candidates.append((match.start(), match.end(), match.group(0)))
        candidates.sort(key=lambda item: item[0])
        return tuple(value for _, _, value in candidates)

    @staticmethod
    def _fallback_expression(query: str) -> str:
        match = re.search(
            r"\b(?:analyse|analyze|review|investigate)\s+(.+?)"
            r"(?:\s+on\s+(?:the\s+)?(?:\d|daily|weekly)|$)",
            query,
            re.IGNORECASE,
        )
        if match is None:
            return query[:160].strip() or "unknown"
        value = re.sub(
            r"\b(?:technically|technical|macro|news|risk)\b",
            "",
            match.group(1),
            flags=re.IGNORECASE,
        )
        return " ".join(value.split())[:160] or "unknown"

    @staticmethod
    def _timeframe(query: str) -> str | None:
        for pattern, timeframe in _TIMEFRAME_PATTERNS:
            if re.search(pattern, query, re.IGNORECASE):
                return timeframe
        return None

    @staticmethod
    def _lookback(query: str) -> ResearchLookback | None:
        match = _LOOKBACK.search(query)
        if match is None:
            return None
        unit = match.group(2).lower().rstrip("s")
        unit = "bars" if unit == "bar" else f"{unit}s"
        return ResearchLookback(value=int(match.group(1)), unit=unit)  # type: ignore[arg-type]

    @staticmethod
    def _intents(query: str, *, has_comparison: bool) -> tuple[ResearchIntent, ...]:
        normalized = query.casefold()
        intents: list[ResearchIntent] = []
        mappings = (
            (ResearchIntent.TECHNICAL, ("technical", "chart", "price action", "trend")),
            (ResearchIntent.MACRO, ("macro", "rates", "inflation", "economy")),
            (ResearchIntent.NEWS, ("news", "event", "catalyst")),
            (ResearchIntent.LIQUIDITY, ("liquidity", "order flow", "positioning")),
            (ResearchIntent.RISK, ("risk", "volatility", "exposure")),
        )
        for intent, keywords in mappings:
            if any(keyword in normalized for keyword in keywords):
                intents.append(intent)
        if has_comparison or _COMPARISON.search(query):
            intents.append(ResearchIntent.COMPARISON)
        if not intents:
            intents.append(ResearchIntent.GENERAL)
        return tuple(dict.fromkeys(intents))

    @staticmethod
    def _unavailable_capabilities(intents: tuple[ResearchIntent, ...]) -> tuple[str, ...]:
        names = {
            ResearchIntent.GENERAL: "completed_market_analysis",
            ResearchIntent.TECHNICAL: "technical_analysis_engine",
            ResearchIntent.MACRO: "macro_analysis_engine",
            ResearchIntent.NEWS: "news_analysis_engine",
            ResearchIntent.LIQUIDITY: "liquidity_analysis_engine",
            ResearchIntent.RISK: "portfolio_risk_analysis_engine",
            ResearchIntent.COMPARISON: "comparative_analysis_engine",
        }
        return tuple(names[intent] for intent in intents)

    @staticmethod
    def _empty_availability(primary: ResolvedResearchInstrument) -> ResearchEvidenceAvailability:
        resolved = primary.status is InstrumentResolutionStatus.RESOLVED
        identity = _availability(
            EvidenceAvailabilityStatus.AVAILABLE if resolved else EvidenceAvailabilityStatus.UNAVAILABLE,
            "INSTRUMENT_IDENTITY_RESOLVED" if resolved else "INSTRUMENT_IDENTITY_NOT_RESOLVED",
            1 if resolved else 0,
        )
        unknown = _availability(EvidenceAvailabilityStatus.UNKNOWN, "EVIDENCE_NOT_ASSESSED")
        return ResearchEvidenceAvailability(
            instrument_identity=identity,
            current_quote=unknown,
            historical_ohlcv=unknown,
            derived_features=unknown,
            news=unknown,
            macro=unknown,
            positioning_order_flow=unknown,
        )

    @staticmethod
    def _quote_availability(snapshot: EvidenceSnapshot, now: datetime) -> EvidenceAvailability:
        quote_evidence = tuple(
            item
            for item in snapshot.market_evidence
            if any(
                value is not None
                for value in (item.price, item.open, item.high, item.low, item.close)
            )
        )
        if not quote_evidence:
            return _availability(EvidenceAvailabilityStatus.UNKNOWN, "CURRENT_QUOTE_NOT_LOADED")
        latest = max(item.timestamp for item in quote_evidence)
        if latest > now:
            return _availability(
                EvidenceAvailabilityStatus.UNKNOWN,
                "QUOTE_TIMESTAMP_FUTURE",
                len(quote_evidence),
            )
        if now - latest > timedelta(minutes=15):
            return _availability(
                EvidenceAvailabilityStatus.STALE,
                "CURRENT_QUOTE_STALE",
                len(quote_evidence),
            )
        return _availability(
            EvidenceAvailabilityStatus.AVAILABLE,
            "CURRENT_QUOTE_PRESENT",
            len(quote_evidence),
        )

    @staticmethod
    def _historical_availability(
        context: ResearchRequestContext,
        snapshot: EvidenceSnapshot,
        now: datetime,
        *,
        historical_retrieval: HistoricalRetrievalDiagnostic | None = None,
    ) -> EvidenceAvailability:
        series = snapshot.market_series
        if context.timeframe is not None:
            series = tuple(item for item in series if item.timeframe == context.timeframe)
        if not series:
            if historical_retrieval is not None:
                status = (
                    EvidenceAvailabilityStatus.NOT_REQUESTED
                    if historical_retrieval.status
                    is HistoricalRetrievalStatus.NOT_REQUESTED
                    else EvidenceAvailabilityStatus.UNAVAILABLE
                )
                return EvidenceAvailability(
                    status=status,
                    record_count=0,
                    reason_codes=historical_retrieval.reason_codes,
                    provider_id=historical_retrieval.provider_id,
                    retrieved_at=historical_retrieval.retrieved_at,
                    missing_interval_count=historical_retrieval.missing_interval_count,
                    provisional_count=historical_retrieval.provisional_count,
                    warning_codes=_diagnostic_warnings(historical_retrieval),
                    error_category=historical_retrieval.error_category,
                )
            reason = (
                "REQUESTED_TIMEFRAME_NOT_LOADED"
                if context.timeframe
                else "HISTORICAL_OHLCV_NOT_LOADED"
            )
            return _availability(EvidenceAvailabilityStatus.UNKNOWN, reason)
        latest = max(
            (item.latest_completed_at for item in series if item.latest_completed_at is not None),
            default=None,
        )
        if latest is None:
            selected = max(series, key=lambda item: item.provenance.latest_received_at)
            return _series_availability(
                selected,
                status=EvidenceAvailabilityStatus.PARTIAL,
                reasons=("HISTORICAL_SERIES_HAS_NO_COMPLETED_BARS",),
                historical_retrieval=historical_retrieval,
            )
        if latest > now:
            selected = max(series, key=lambda item: item.provenance.latest_received_at)
            return _series_availability(
                selected,
                status=EvidenceAvailabilityStatus.UNKNOWN,
                reasons=("HISTORICAL_TIMESTAMP_FUTURE",),
                historical_retrieval=historical_retrieval,
            )
        selected = max(series, key=lambda item: item.provenance.latest_received_at)
        if context.timeframe is not None:
            threshold = timedelta(seconds=_TIMEFRAME_SECONDS[context.timeframe] * 2)
            if now - latest > threshold:
                return _series_availability(
                    selected,
                    status=EvidenceAvailabilityStatus.STALE,
                    reasons=("HISTORICAL_OHLCV_STALE",),
                    historical_retrieval=historical_retrieval,
                )
        partial = (
            selected.continuity.missing_interval_count > 0
            or selected.provisional_count > 0
            or (historical_retrieval is not None and historical_retrieval.partial)
        )
        reasons = ["HISTORICAL_OHLCV_PRESENT"]
        if selected.continuity.missing_interval_count:
            reasons.append("HISTORICAL_INTERVALS_MISSING")
        if selected.provisional_count:
            reasons.append("HISTORICAL_PROVISIONAL_BAR_PRESENT")
        return _series_availability(
            selected,
            status=(
                EvidenceAvailabilityStatus.PARTIAL
                if partial
                else EvidenceAvailabilityStatus.AVAILABLE
            ),
            reasons=tuple(reasons),
            historical_retrieval=historical_retrieval,
        )


def _reference_for_key(key: str) -> _ReferenceSubject | None:
    return next(
        (
            subject
            for subject in _REFERENCE_SUBJECTS
            if any(_normalized_match_key(alias) == key for alias in subject.aliases)
        ),
        None,
    )


def _comparison_expressions(query: str) -> tuple[str, ...]:
    """Retain both comparison operands, including unknown or ambiguous subjects."""
    if _COMPARISON.search(query) is None:
        return ()
    body = query.strip().rstrip(".?!")
    compare_prefix = re.search(r"\bcompare\b", body, re.IGNORECASE)
    if compare_prefix is not None:
        body = body[compare_prefix.end() :].strip()
    else:
        body = re.sub(
            r"^\s*(?:analyse|analyze|review|investigate)\s+",
            "",
            body,
            count=1,
            flags=re.IGNORECASE,
        )
    parts = re.split(
        r"\s+(?:and|versus|vs\.?|against|with)\s+",
        body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    if len(parts) != 2:
        return ()
    right = re.split(
        r"\s+on\s+(?:the\s+)?(?=\d|daily\b|weekly\b)",
        parts[1],
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    operands = tuple(" ".join(value.split())[:160] for value in (parts[0], right))
    return operands if all(operands) else ()


def _from_catalogue(expression: str, entry: InstrumentCatalogueEntry) -> ResolvedResearchInstrument:
    return ResolvedResearchInstrument(
        expression=expression,
        status=InstrumentResolutionStatus.RESOLVED,
        identity_source=InstrumentIdentitySource.CATALOGUE,
        instrument_id=entry.instrument_id,
        canonical_instrument_id=entry.canonical_instrument_id,
        canonical_symbol=entry.canonical_symbol,
        category=entry.category,
        display_name=entry.display_name,
        base_currency=entry.base_currency,
        quote_currency=entry.quote_currency,
        exchange_id=entry.exchange_id,
        exchange_resolution=entry.exchange_resolution,
        candidate_instrument_ids=(entry.instrument_id,),
        reason_codes=("EXACT_CATALOGUE_IDENTITY_RESOLVED",),
    )


def _ambiguous(
    expression: str,
    entries: tuple[InstrumentCatalogueEntry, ...],
    reason: str,
) -> ResolvedResearchInstrument:
    return ResolvedResearchInstrument(
        expression=expression,
        status=InstrumentResolutionStatus.AMBIGUOUS,
        identity_source=InstrumentIdentitySource.NONE,
        candidate_instrument_ids=tuple(sorted(item.instrument_id for item in entries)),
        reason_codes=(reason,),
    )


def _unresolved(expression: str, reason: str) -> ResolvedResearchInstrument:
    return ResolvedResearchInstrument(
        expression=expression,
        status=InstrumentResolutionStatus.UNRESOLVED,
        identity_source=InstrumentIdentitySource.NONE,
        reason_codes=(reason,),
    )


def _entry_search_values(entry: InstrumentCatalogueEntry) -> tuple[str, ...]:
    return tuple(
        value
        for value in (
            entry.instrument_id,
            entry.canonical_instrument_id,
            entry.provider_symbol,
            entry.canonical_symbol,
            entry.display_symbol,
            entry.display_name,
            *(alias.alias for alias in entry.aliases),
            *(mapping.provider_symbol for mapping in entry.provider_mappings),
            *(mapping.provider_instrument_id for mapping in entry.provider_mappings),
        )
        if value
    )


def _entry_match_keys(entry: InstrumentCatalogueEntry) -> set[str]:
    return {_normalized_match_key(value) for value in _entry_search_values(entry)}


def _deduplicate_entries(
    entries: Iterable[InstrumentCatalogueEntry],
) -> tuple[InstrumentCatalogueEntry, ...]:
    by_identity = {entry.instrument_id: entry for entry in entries}
    return tuple(by_identity[key] for key in sorted(by_identity))


def _normalized_match_key(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _resolution_identity(value: ResolvedResearchInstrument) -> tuple[str, str]:
    return (
        value.instrument_id or "",
        value.canonical_symbol or _normalized_match_key(value.expression),
    )


def _availability(
    status: EvidenceAvailabilityStatus,
    reason: str,
    count: int = 0,
) -> EvidenceAvailability:
    return EvidenceAvailability(status=status, record_count=count, reason_codes=(reason,))


def _series_availability(
    series,
    *,
    status: EvidenceAvailabilityStatus,
    reasons: tuple[str, ...],
    historical_retrieval: HistoricalRetrievalDiagnostic | None,
) -> EvidenceAvailability:
    diagnostic_reasons = (
        historical_retrieval.reason_codes if historical_retrieval is not None else ()
    )
    warning_codes = list(_diagnostic_warnings(historical_retrieval))
    return EvidenceAvailability(
        status=status,
        record_count=series.bar_count,
        reason_codes=tuple(dict.fromkeys((*reasons, *diagnostic_reasons))),
        provider_id=series.provenance.source_id,
        coverage_start=series.bars[0].opened_at,
        coverage_end=series.bars[-1].closed_at,
        last_source_candle_at=series.bars[-1].opened_at,
        retrieved_at=series.provenance.latest_received_at,
        missing_interval_count=series.continuity.missing_interval_count,
        provisional_count=series.provisional_count,
        warning_codes=tuple(dict.fromkeys(warning_codes)),
        error_category=(
            historical_retrieval.error_category
            if historical_retrieval is not None
            else None
        ),
    )


def _diagnostic_warnings(
    diagnostic: HistoricalRetrievalDiagnostic | None,
) -> tuple[str, ...]:
    if diagnostic is None or not diagnostic.partial:
        return ()
    warning_markers = ("EMPTY", "SHORT", "MISSING", "PROVISIONAL", "BOUNDED")
    return tuple(
        code
        for code in diagnostic.reason_codes
        if any(marker in code for marker in warning_markers)
    )


def _presence(items: tuple[object, ...], prefix: str) -> EvidenceAvailability:
    if items:
        return _availability(
            EvidenceAvailabilityStatus.AVAILABLE,
            f"{prefix}_PRESENT",
            len(items),
        )
    return _availability(EvidenceAvailabilityStatus.UNKNOWN, f"{prefix}_NOT_LOADED")
