from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
import ssl

import httpx
import pytest

from app.connectors.bls import BlsMacroProvider
from app.connectors.federal_reserve import FederalReserveMacroProvider
from app.connectors.fred import FredMacroProvider
from app.connectors.macro_adapter import MacroProviderAdapter
from app.connectors.macro_http import MacroHttpTransport, MacroProviderError
from app.connectors.macro_normalization import observation_from_mapping
from app.connectors.trading_economics import TradingEconomicsMacroProvider
from app.connectors.us_treasury import TreasuryMacroProvider
from app.evidence.store import EvidenceStore
from app.models.economic_events import (
    EconomicEventSource,
    EconomicImpact,
    EconomicReleaseStatus,
    EconomicValue,
    EconomicValueProvenance,
    EconomicValueUnit,
    NormalizedEconomicEvent,
)
from app.models.events import DataDomain, SourceTransport
from app.models.macro_intelligence import MacroProviderDiagnostic, MacroProviderStatus, MacroSourceType
from app.models.macro_providers import (
    MacroProviderBatch,
    MacroProviderConfiguration,
    MacroProviderFailureCategory,
    MacroProviderId,
    MacroProviderOperation,
    MacroProviderRequest,
)
from app.services.macro_provider_registry import mapping_for_indicator
from app.services.macro_intelligence import MacroIntelligenceService
from app.services.macro_retrieval import MacroRetrievalService


NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def _configuration(provider: MacroProviderId, *, enabled: bool = True, credential: bool = False):
    return MacroProviderConfiguration(
        provider_id=provider,
        enabled=enabled,
        credential_configured=credential,
        commercial_authorization=(provider is MacroProviderId.TRADING_ECONOMICS and enabled),
        redistribution_authorization=(provider is MacroProviderId.TRADING_ECONOMICS and enabled),
        request_budget=20,
    )


def _request(provider: MacroProviderId, *indicators: str) -> MacroProviderRequest:
    return MacroProviderRequest(
        provider_id=provider,
        operation=MacroProviderOperation.REFRESH,
        indicator_ids=indicators,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 11, 5),
        requested_at=datetime.now(UTC),
    )


class _Transport:
    def __init__(self, *, json_payload=None, xml_payload: bytes | None = None, error=None):
        self.json_payload = json_payload
        self.xml_payload = xml_payload
        self.error = error
        self.calls = 0
        self.last_params = None
        self.last_payload = None

    async def get_json(self, _url, *, params=None, headers=None):
        self.calls += 1
        self.last_params = params
        if self.error:
            raise self.error
        return self.json_payload

    async def post_json(self, _url, *, payload, headers=None):
        self.calls += 1
        self.last_payload = payload
        if self.error:
            raise self.error
        return self.json_payload

    async def get_xml(self, _url, *, params=None, headers=None):
        self.calls += 1
        self.last_params = params
        if self.error:
            raise self.error
        return self.xml_payload


def test_fred_preserves_original_publisher_and_revision_lineage() -> None:
    transport = _Transport(json_payload={"observations": [
        {"date": "2026-09-01", "realtime_start": "2026-10-10", "value": "310.1"},
        {"date": "2026-09-01", "realtime_start": "2026-10-20", "value": "310.2"},
    ]})
    provider = FredMacroProvider(
        _configuration(MacroProviderId.FRED, credential=True), api_key="not-a-real-key", transport=transport
    )
    batch = asyncio.run(provider.fetch(_request(MacroProviderId.FRED, "us_cpi_all_items_index")))
    assert transport.calls == 1
    assert len(batch.observations) == 2
    first, second = batch.observations
    assert first.source.source_id == "fred"
    assert first.source.origin_id == "bls"
    assert second.observation_version == 2
    assert second.revision_of_observation_id == first.observation_id
    assert batch.diagnostic.status is MacroProviderStatus.AVAILABLE


def test_fred_suppresses_exact_duplicate_vintage_rows() -> None:
    row = {"date": "2026-09-01", "realtime_start": "2026-10-10", "value": "310.1"}
    transport = _Transport(json_payload={"observations": [row, dict(row)]})
    provider = FredMacroProvider(
        _configuration(MacroProviderId.FRED, credential=True), api_key="not-a-real-key", transport=transport
    )
    batch = asyncio.run(provider.fetch(_request(MacroProviderId.FRED, "us_cpi_all_items_index")))
    assert len(batch.observations) == 1
    assert batch.diagnostic.received_count == 2
    assert batch.diagnostic.validated_count == 1


def test_treasury_normalizes_nominal_and_real_yields_without_publication_guess() -> None:
    xml = b'''<feed xmlns:m="urn:m" xmlns:d="urn:d"><entry><content><m:properties>
      <d:NEW_DATE>2026-11-04T00:00:00</d:NEW_DATE><d:BC_10YEAR>4.10</d:BC_10YEAR>
    </m:properties></content></entry></feed>'''
    transport = _Transport(xml_payload=xml)
    provider = TreasuryMacroProvider(_configuration(MacroProviderId.US_TREASURY), transport=transport)
    batch = asyncio.run(provider.fetch(_request(MacroProviderId.US_TREASURY, "us_nominal_treasury_yield_10y")))
    assert len(batch.observations) == 1
    item = batch.observations[0]
    assert item.value.value == Decimal("4.10")
    assert item.published_at is None
    assert item.source.origin_id == "us_treasury"


def test_bls_v1_skips_annual_average_and_preserves_footnote_diagnostic() -> None:
    transport = _Transport(json_payload={
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": "CUSR0000SA0", "data": [
            {"year": "2026", "period": "M09", "value": "310.1", "footnotes": [{"text": "preliminary"}]},
            {"year": "2026", "period": "M13", "value": "309.0", "footnotes": []},
        ]}]},
    })
    provider = BlsMacroProvider(_configuration(MacroProviderId.BLS), transport=transport)
    batch = asyncio.run(provider.fetch(_request(MacroProviderId.BLS, "us_cpi_all_items_index")))
    assert transport.calls == 1
    assert "registrationkey" not in transport.last_payload
    assert len(batch.observations) == 1
    assert "SOURCE_FOOTNOTES_PRESENT" in batch.diagnostic.reason_codes


def test_federal_reserve_documents_keep_decision_values_unknown() -> None:
    xml = b'''<rss><channel><item><title>Federal Reserve issues FOMC statement</title>
      <guid>https://example.invalid/fomc-1</guid><pubDate>Thu, 05 Nov 2026 14:00:00 GMT</pubDate>
    </item></channel></rss>'''
    provider = FederalReserveMacroProvider(
        _configuration(MacroProviderId.FEDERAL_RESERVE), transport=_Transport(xml_payload=xml)
    )
    batch = asyncio.run(provider.fetch(_request(MacroProviderId.FEDERAL_RESERVE, "fed_policy_rate")))
    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.actual is None and event.forecast is None
    assert "POLICY_DOCUMENTS_ONLY" in batch.diagnostic.reason_codes


def test_trading_economics_is_disabled_without_explicit_authorization() -> None:
    provider = TradingEconomicsMacroProvider(
        _configuration(MacroProviderId.TRADING_ECONOMICS, enabled=False), api_key=None
    )
    batch = asyncio.run(provider.fetch(_request(MacroProviderId.TRADING_ECONOMICS, "us_manufacturing_pmi")))
    assert batch.diagnostic.status is MacroProviderStatus.NOT_CONFIGURED
    assert batch.diagnostic.request_count == 0
    assert not batch.observations and not batch.events


def test_forecast_provenance_survives_evidence_ingestion_without_becoming_actual() -> None:
    provenance = EconomicValueProvenance(
        retrieval_provider_id="licensed-calendar",
        retrieval_provider_name="Licensed Calendar",
        original_publisher_id="consensus-panel",
        original_publisher_name="Consensus Panel",
        role="consensus",
        retrieved_at=NOW,
    )
    event = NormalizedEconomicEvent(
        source=EconomicEventSource(
            source_id="licensed-calendar",
            display_name="Licensed Calendar",
            transport=SourceTransport.API,
            domain=DataDomain.ECONOMIC_DATA,
            origin_id="bls",
            origin_name="U.S. Bureau of Labor Statistics",
        ),
        occurrence_id="cpi-2026-10",
        event_name="US CPI",
        indicator_id="us_cpi_all_items_index",
        country="US",
        currency="USD",
        scheduled_at=NOW,
        release_status=EconomicReleaseStatus.SCHEDULED,
        expected_impact=EconomicImpact.HIGH,
        forecast=EconomicValue(value=Decimal("3.1"), unit=EconomicValueUnit.PERCENT),
        forecast_provenance=provenance,
        actual=None,
        release_version=1,
        received_at=NOW,
    )
    store = EvidenceStore()
    retained = store.ingest_economic_event(event)
    assert retained.forecast_provenance == provenance
    assert retained.actual is None and retained.actual_provenance is None
    assert retained.provenance.origin_id == "bls"


class _Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.response


def test_http_transport_uses_explicit_tls_no_environment_and_no_retry() -> None:
    captured = {}
    response = httpx.Response(429, headers={"Retry-After": "17"}, content=b"ignored")

    def factory(**kwargs):
        captured.update(kwargs)
        return _Client(response=response)

    transport = MacroHttpTransport(timeout_seconds=7, client_factory=factory)
    with pytest.raises(MacroProviderError) as raised:
        asyncio.run(transport.get_json("https://example.invalid", params={"safe": "value"}))
    error = raised.value
    assert isinstance(captured["verify"], ssl.SSLContext)
    assert captured["verify"].verify_mode == ssl.CERT_REQUIRED
    assert captured["trust_env"] is False
    assert error.category is MacroProviderFailureCategory.RATE_LIMIT
    assert error.retry_after_seconds == 17 and error.request_sent


def test_http_timeout_stage_distinguishes_connect_from_response_wait() -> None:
    for exception, stage, sent in (
        (httpx.ConnectTimeout("connect"), "connection", False),
        (httpx.ReadTimeout("read"), "response_wait", True),
    ):
        calls = 0

        def factory(**_kwargs):
            nonlocal calls
            calls += 1
            return _Client(error=exception)

        with pytest.raises(MacroProviderError) as raised:
            asyncio.run(MacroHttpTransport(client_factory=factory).get_json("https://example.invalid"))
        assert calls == 1
        assert raised.value.failure_stage == stage
        assert raised.value.request_sent is sent


@pytest.mark.parametrize(
    ("status", "category", "reason"),
    (
        (401, MacroProviderFailureCategory.AUTHENTICATION, "AUTHENTICATION_REJECTED"),
        (400, MacroProviderFailureCategory.INVALID_REQUEST, "HTTP_INVALID_REQUEST"),
        (503, MacroProviderFailureCategory.SERVER_ERROR, "HTTP_SERVER_ERROR"),
    ),
)
def test_http_statuses_have_safe_categories_without_retry(status, category, reason) -> None:
    calls = 0

    def factory(**_kwargs):
        nonlocal calls
        calls += 1
        return _Client(response=httpx.Response(status, content=b"sensitive body is ignored"))

    with pytest.raises(MacroProviderError) as raised:
        asyncio.run(MacroHttpTransport(client_factory=factory).get_json("https://example.invalid"))
    assert calls == 1
    assert raised.value.category is category
    assert str(raised.value) == reason
    assert "sensitive" not in str(raised.value)


def test_invalid_json_is_malformed_and_never_retains_body() -> None:
    def factory(**_kwargs):
        return _Client(response=httpx.Response(200, content=b"private invalid payload"))

    with pytest.raises(MacroProviderError) as raised:
        asyncio.run(MacroHttpTransport(client_factory=factory).get_json("https://example.invalid"))
    assert raised.value.category is MacroProviderFailureCategory.MALFORMED_RESPONSE
    assert str(raised.value) == "INVALID_JSON"


class _SequenceAdapter(MacroProviderAdapter):
    provider_id = MacroProviderId.FRED
    source_type = MacroSourceType.OFFICIAL_OBSERVATION

    def __init__(self):
        super().__init__(_configuration(MacroProviderId.FRED, credential=True))
        self.calls = 0

    async def fetch(self, request):
        self.calls += 1
        await asyncio.sleep(0)
        if self.calls > 1:
            diagnostic = MacroProviderDiagnostic(
                provider_id="fred", source_type=self.source_type, status=MacroProviderStatus.RATE_LIMITED,
                requested_at=request.requested_at, request_count=1, reason_codes=("HTTP_RATE_LIMIT",),
                error_category="rate_limit", failure_stage="provider_response", http_status_code=429,
                request_sent=True,
            )
            return MacroProviderBatch(provider_id=self.provider_id, operation=request.operation, diagnostic=diagnostic, cache_preserved=True)
        mapping = mapping_for_indicator(self.provider_id, "us_cpi_all_items_index")
        assert mapping is not None
        observation = observation_from_mapping(
            mapping, provider_name="FRED/ALFRED", reference_date=date(2026, 9, 1),
            raw_value="310.1", retrieved_at=request.requested_at, published_at=NOW,
        )
        diagnostic = MacroProviderDiagnostic(
            provider_id="fred", source_type=self.source_type, status=MacroProviderStatus.AVAILABLE,
            requested_at=request.requested_at, retrieved_at=request.requested_at, request_count=1, received_count=1,
            validated_count=1, request_sent=True,
        )
        return MacroProviderBatch(
            provider_id=self.provider_id, operation=request.operation, observations=(observation,),
            diagnostic=diagnostic, retrieved_at=request.requested_at,
        )


def test_retrieval_single_flight_and_failure_preserves_cached_evidence() -> None:
    adapter = _SequenceAdapter()
    store = EvidenceStore()
    service = MacroRetrievalService(evidence_store=store, adapters={MacroProviderId.FRED: adapter})
    async def run():
        first, duplicate = await asyncio.gather(
            service.refresh(MacroProviderId.FRED, ("us_cpi_all_items_index",)),
            service.refresh(MacroProviderId.FRED, ("us_cpi_all_items_index",)),
        )
        failed = await service.refresh(
            MacroProviderId.FRED, ("us_cpi_all_items_index",), start_date=date(2026, 1, 1)
        )
        return first, duplicate, failed

    first, duplicate, failed = asyncio.run(run())
    # Two coalesced calls plus one distinct-window call produce two fetches, not three.
    assert adapter.calls == 2
    assert first == duplicate
    assert len(store.snapshot().macro_series) == 1
    assert failed.diagnostic.status is MacroProviderStatus.RATE_LIMITED
    assert failed.cache_preserved and failed.diagnostic.cached_evidence_count == 1
    assert len(store.snapshot().macro_series) == 1
    report = MacroIntelligenceService().analyze(
        subject="XAU/USD", snapshot=store.snapshot(), evaluated_at=first.diagnostic.retrieved_at
    )
    assert len(report.observations) == 1
    summary = report.observations[0]
    assert summary.retrieval_provider_id == "fred"
    assert summary.original_publisher_id == "bls"
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False
