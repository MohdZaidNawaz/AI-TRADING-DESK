from datetime import UTC, date, datetime

import pytest

from app.agents import create_friday_core
from app.catalogue.store import InstrumentCatalogueStore, read_catalogue_snapshot
from app.models.instruments import (
    ExchangeResolution,
    InstrumentAlias,
    InstrumentCatalogueEntry,
    InstrumentCategory,
    InstrumentContractKind,
    InstrumentProviderMapping,
    InstrumentSourceProvenance,
    canonical_instrument_id,
)
from app.models.research_requests import (
    EvidenceAvailabilityStatus,
    InstrumentIdentitySource,
    InstrumentResolutionStatus,
    ResearchIntent,
)
from app.models.schemas import AnalyzeRequest
from app.services.research_requests import ResearchRequestService


NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("expression", "canonical_symbol", "category"),
    (
        ("XAUUSD", "XAU/USD", InstrumentCategory.COMMODITY),
        ("Gold", "XAU/USD", InstrumentCategory.COMMODITY),
        ("Spot Gold", "XAU/USD", InstrumentCategory.COMMODITY),
        ("XAGUSD", "XAG/USD", InstrumentCategory.COMMODITY),
        ("Silver", "XAG/USD", InstrumentCategory.COMMODITY),
        ("EURUSD", "EUR/USD", InstrumentCategory.FOREX),
        ("GBPUSD", "GBP/USD", InstrumentCategory.FOREX),
        ("BTCUSD", "BTC/USD", InstrumentCategory.CRYPTO),
        ("ETHUSD", "ETH/USD", InstrumentCategory.CRYPTO),
    ),
)
def test_standard_research_subjects_resolve_without_inventing_catalogue_membership(
    expression: str,
    canonical_symbol: str,
    category: InstrumentCategory,
) -> None:
    resolved = ResearchRequestService().resolve_instrument(expression)

    assert resolved.status is InstrumentResolutionStatus.RESOLVED
    assert resolved.identity_source is InstrumentIdentitySource.STANDARD_REFERENCE
    assert resolved.canonical_symbol == canonical_symbol
    assert resolved.category is category
    assert resolved.instrument_id is None
    assert resolved.canonical_instrument_id is None
    assert resolved.exchange_id is None
    assert "CATALOGUE_IDENTITY_NOT_PRESENT" in resolved.reason_codes


def test_usoil_remains_ambiguous_without_contract_or_provider_identity() -> None:
    context = ResearchRequestService().prepare(AnalyzeRequest(query="Analyse USOIL"))

    assert context.primary_instrument.status is InstrumentResolutionStatus.AMBIGUOUS
    assert context.primary_instrument.canonical_symbol is None
    assert context.primary_instrument.reason_codes == (
        "CONTRACT_OR_PROVIDER_IDENTITY_REQUIRED",
    )
    assert "instrument_identity_ambiguous" in context.missing_information


def test_unknown_instrument_fails_closed() -> None:
    context = ResearchRequestService().prepare(
        AnalyzeRequest(query="Analyse UNKNOWN_WIDGET")
    )

    assert context.primary_instrument.status is InstrumentResolutionStatus.UNRESOLVED
    assert context.primary_instrument.reason_codes == ("INSTRUMENT_NOT_FOUND",)
    assert context.evidence.instrument_identity.status is EvidenceAvailabilityStatus.UNAVAILABLE
    assert context.production_authorization_available is False
    assert context.submission_allowed is False
    assert context.execution_authority is False


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("Analyse Gold on the 1-minute timeframe", "1m"),
        ("Analyse Gold on 5m", "5m"),
        ("Analyse Gold on the 15-minute timeframe", "15m"),
        ("Analyse Gold on 30m", "30m"),
        ("Analyse EURUSD on the 1-hour timeframe", "1h"),
        ("Analyse GBPJPY on the 4-hour timeframe", "4h"),
        ("Analyse Bitcoin daily", "1d"),
        ("Analyse Ethereum weekly", "1w"),
    ),
)
def test_supported_timeframes_are_extracted(query: str, expected: str) -> None:
    assert ResearchRequestService().prepare(AnalyzeRequest(query=query)).timeframe == expected


def test_missing_timeframe_remains_unspecified() -> None:
    context = ResearchRequestService().prepare(AnalyzeRequest(query="Analyse XAUUSD"))

    assert context.timeframe is None
    assert "timeframe_unspecified" in context.missing_information


def test_lookback_is_structured_without_inventing_timeframe() -> None:
    context = ResearchRequestService().prepare(
        AnalyzeRequest(query="Analyse XAUUSD for the last 30 days")
    )

    assert context.lookback is not None
    assert context.lookback.value == 30
    assert context.lookback.unit == "days"
    assert context.timeframe is None


def test_comparison_request_resolves_both_subjects() -> None:
    context = ResearchRequestService().prepare(
        AnalyzeRequest(query="Compare Gold and Silver")
    )

    assert context.primary_instrument.canonical_symbol == "XAU/USD"
    assert [item.canonical_symbol for item in context.comparison_instruments] == ["XAG/USD"]
    assert context.primary_instrument.status is InstrumentResolutionStatus.RESOLVED
    assert context.comparison_instruments[0].status is InstrumentResolutionStatus.RESOLVED
    assert context.timeframe is None
    assert ResearchIntent.COMPARISON in context.intents
    assert "comparative_analysis_engine" in context.unavailable_capabilities


def test_comparison_api_response_contains_both_resolved_instruments() -> None:
    response = create_friday_core().analyze(
        AnalyzeRequest(query="Compare Gold and Silver")
    )
    context = response.routing.research_context

    assert context is not None
    assert response.symbol == "XAU/USD"
    assert context.primary_instrument.canonical_symbol == "XAU/USD"
    assert context.primary_instrument.status is InstrumentResolutionStatus.RESOLVED
    assert len(context.comparison_instruments) == 1
    silver = context.comparison_instruments[0]
    assert silver.canonical_symbol == "XAG/USD"
    assert silver.status is InstrumentResolutionStatus.RESOLVED
    assert silver.instrument_id is None
    assert silver.exchange_id is None
    assert context.timeframe is None


def test_ambiguous_comparison_instrument_is_retained() -> None:
    context = ResearchRequestService().prepare(
        AnalyzeRequest(query="Compare Gold and USOIL")
    )

    assert context.primary_instrument.canonical_symbol == "XAU/USD"
    assert len(context.comparison_instruments) == 1
    comparison = context.comparison_instruments[0]
    assert comparison.expression == "USOIL"
    assert comparison.status is InstrumentResolutionStatus.AMBIGUOUS
    assert comparison.reason_codes == ("CONTRACT_OR_PROVIDER_IDENTITY_REQUIRED",)
    assert context.timeframe is None


def test_unresolved_comparison_instrument_is_retained() -> None:
    context = ResearchRequestService().prepare(
        AnalyzeRequest(query="Compare Gold and PLATINUM_WIDGET")
    )

    assert context.primary_instrument.canonical_symbol == "XAU/USD"
    assert len(context.comparison_instruments) == 1
    comparison = context.comparison_instruments[0]
    assert comparison.expression == "PLATINUM_WIDGET"
    assert comparison.status is InstrumentResolutionStatus.UNRESOLVED
    assert comparison.reason_codes == ("INSTRUMENT_NOT_FOUND",)
    assert context.timeframe is None


def test_duplicate_catalogue_symbols_on_different_exchanges_are_ambiguous() -> None:
    entries = (
        _entry(InstrumentCategory.STOCK, "ABC", "XNYS"),
        _entry(InstrumentCategory.STOCK, "ABC", "XNAS"),
    )
    resolved = ResearchRequestService(catalogue_entries=entries).resolve_instrument("ABC")

    assert resolved.status is InstrumentResolutionStatus.AMBIGUOUS
    assert resolved.reason_codes == ("MULTIPLE_CATALOGUE_IDENTITIES",)
    assert resolved.candidate_instrument_ids == tuple(sorted(item.instrument_id for item in entries))


def test_exact_catalogue_identity_resolves_all_supported_asset_classes() -> None:
    entries = tuple(
        _entry(category, f"TEST{index}", f"X{index:03d}")
        for index, category in enumerate(InstrumentCategory, start=1)
    )
    service = ResearchRequestService(catalogue_entries=entries)

    for entry in entries:
        resolved = service.resolve_instrument(entry.instrument_id)
        assert resolved.status is InstrumentResolutionStatus.RESOLVED
        assert resolved.identity_source is InstrumentIdentitySource.CATALOGUE
        assert resolved.instrument_id == entry.instrument_id
        assert resolved.category is entry.category


def test_legacy_unresolved_alias_is_ambiguous_but_legacy_id_still_resolves() -> None:
    legacy = _entry(
        InstrumentCategory.STOCK,
        "LEGACY",
        "LEGACY_UNRESOLVED:ABC123",
        exchange_resolution=ExchangeResolution.LEGACY_UNRESOLVED,
    )
    service = ResearchRequestService(catalogue_entries=(legacy,))

    assert service.resolve_instrument("LEGACY").status is InstrumentResolutionStatus.AMBIGUOUS
    exact = service.resolve_instrument(legacy.instrument_id)
    assert exact.status is InstrumentResolutionStatus.RESOLVED
    assert exact.exchange_resolution is ExchangeResolution.LEGACY_UNRESOLVED


def test_ready_v2_catalogue_can_be_loaded_without_refresh_or_provider_access(tmp_path) -> None:
    path = tmp_path / "catalogue.sqlite3"
    entry = _entry(InstrumentCategory.ETF, "SPY", "ARCX")
    InstrumentCatalogueStore(path).replace_snapshot((entry,), refreshed_at=NOW)

    loaded = read_catalogue_snapshot(path)

    assert loaded == (entry,)


def test_friday_core_adds_context_without_breaking_top_level_api_contract() -> None:
    response = create_friday_core().analyze(
        AnalyzeRequest(query="Analyse Gold on the 15-minute timeframe")
    )

    assert set(response.model_dump()) == {
        "analysis_id",
        "mode",
        "symbol",
        "query",
        "selected_agents",
        "routing",
        "agent_results",
        "synthesis",
            "coordination",
    }
    assert response.mode == "deterministic_mock"
    assert response.symbol == "XAU/USD"
    context = response.routing.research_context
    assert context is not None
    assert context.primary_instrument.canonical_symbol == "XAU/USD"
    assert context.timeframe == "15m"
    assert context.analysis_status == "research_preparation_only"
    assert context.evidence.current_quote.status is EvidenceAvailabilityStatus.UNKNOWN
    assert context.evidence.historical_ohlcv.status is EvidenceAvailabilityStatus.UNKNOWN
    assert context.evidence.news.status is EvidenceAvailabilityStatus.UNKNOWN
    assert context.production_authorization_available is False
    assert context.submission_allowed is False
    assert context.execution_authority is False


def test_ambiguous_request_marks_all_dependent_evidence_unavailable() -> None:
    response = create_friday_core().analyze(AnalyzeRequest(query="Analyse USOIL"))
    context = response.routing.research_context

    assert context is not None
    assert response.symbol is None
    assert context.primary_instrument.status is InstrumentResolutionStatus.AMBIGUOUS
    dependent = context.evidence.model_dump(exclude={"instrument_identity"})
    assert {item["status"] for item in dependent.values()} == {"unavailable"}


def _entry(
    category: InstrumentCategory,
    symbol: str,
    exchange_id: str,
    *,
    exchange_resolution: ExchangeResolution = ExchangeResolution.RESOLVED,
) -> InstrumentCatalogueEntry:
    contract_kind = (
        InstrumentContractKind.DATED
        if category is InstrumentCategory.FUTURE
        else InstrumentContractKind.NONE
    )
    contract_expiry = date(2027, 3, 19) if category is InstrumentCategory.FUTURE else None
    canonical_id = canonical_instrument_id(
        category,
        exchange_id,
        symbol,
        contract_kind=contract_kind,
        contract_expiry=contract_expiry,
    )
    instrument_id = f"test:{category.value}:{exchange_id}:{symbol}"
    return InstrumentCatalogueEntry(
        instrument_id=instrument_id,
        canonical_instrument_id=canonical_id,
        source_id="test",
        category=category,
        provider_symbol=symbol,
        canonical_symbol=symbol,
        exchange_id=exchange_id,
        exchange_resolution=exchange_resolution,
        contract_kind=contract_kind,
        contract_expiry=contract_expiry,
        display_symbol=symbol,
        display_name=f"{symbol} instrument",
        provider_mappings=(
            InstrumentProviderMapping(
                source_id="test",
                provider_symbol=symbol,
                provider_exchange_id=exchange_id,
                provider_contract_id=(contract_expiry.isoformat() if contract_expiry else "NONE"),
                provider_instrument_id=instrument_id,
            ),
        ),
        aliases=(
            InstrumentAlias(
                alias=symbol,
                exchange_id=exchange_id,
                source_id="test",
            ),
        ),
        source_provenance=(
            InstrumentSourceProvenance(
                source_id="test",
                source_record_id=instrument_id,
                observed_at=NOW,
            ),
        ),
        created_at=NOW,
        updated_at=NOW,
    )
