"""Federal Reserve official-document adapter for released policy communications."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from app.connectors.macro_adapter import MacroProviderAdapter
from app.connectors.macro_http import MacroHttpTransport, MacroProviderError
from app.connectors.macro_normalization import deduplicate_events
from app.models.economic_events import (
    EconomicEventSource,
    EconomicImpact,
    EconomicReleaseStatus,
    NormalizedEconomicEvent,
)
from app.models.events import DataDomain, SourceTransport
from app.models.macro_intelligence import MacroProviderDiagnostic, MacroProviderStatus, MacroSourceType
from app.models.macro_providers import (
    MacroProviderBatch,
    MacroProviderConfiguration,
    MacroProviderFailureCategory,
    MacroProviderId,
    MacroProviderRequest,
)


FED_MONETARY_POLICY_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"


class FederalReserveMacroProvider(MacroProviderAdapter):
    provider_id = MacroProviderId.FEDERAL_RESERVE
    source_type = MacroSourceType.CENTRAL_BANK

    def __init__(
        self,
        configuration: MacroProviderConfiguration,
        *,
        transport: MacroHttpTransport | None = None,
    ) -> None:
        super().__init__(configuration)
        self._transport = transport or MacroHttpTransport(
            timeout_seconds=configuration.timeout_seconds
        )

    async def fetch(self, request: MacroProviderRequest) -> MacroProviderBatch:
        if request.provider_id is not self.provider_id:
            raise ValueError("Federal Reserve request provider mismatch")
        if not self.configuration.enabled:
            return self._not_configured(request, "FEDERAL_RESERVE_PROVIDER_DISABLED")
        if "fed_policy_rate" not in request.indicator_ids:
            return self._not_configured(request, "FEDERAL_RESERVE_MAPPING_UNAVAILABLE")
        retrieved_at = datetime.now(UTC)
        try:
            payload = await self._transport.get_xml(FED_MONETARY_POLICY_RSS)
            events = deduplicate_events(
                _rss_events(payload, retrieved_at=retrieved_at, max_records=request.max_records)
            )
        except MacroProviderError as error:
            return self._failure(request, error)
        except (ElementTree.ParseError, TypeError, ValueError):
            return self._failure(
                request,
                MacroProviderError(
                    MacroProviderFailureCategory.MALFORMED_RESPONSE,
                    reason_code="FEDERAL_RESERVE_FEED_INVALID",
                    failure_stage="response_validation",
                    request_sent=True,
                ),
            )
        diagnostic = MacroProviderDiagnostic(
            provider_id=self.provider_id.value,
            source_type=self.source_type,
            status=MacroProviderStatus.AVAILABLE,
            requested_at=request.requested_at,
            retrieved_at=retrieved_at,
            request_count=1,
            received_count=len(events),
            validated_count=len(events),
            reason_codes=("POLICY_DOCUMENTS_ONLY",),
            request_sent=True,
            new_evidence_count=len(events),
        )
        return MacroProviderBatch(
            provider_id=self.provider_id,
            operation=request.operation,
            events=events,
            diagnostic=diagnostic,
            retrieved_at=retrieved_at,
        )


def _rss_events(
    payload: bytes,
    *,
    retrieved_at: datetime,
    max_records: int,
) -> tuple[NormalizedEconomicEvent, ...]:
    root = ElementTree.fromstring(payload)
    source = EconomicEventSource(
        source_id="federal_reserve",
        display_name="Board of Governors of the Federal Reserve System",
        transport=SourceTransport.FEED,
        domain=DataDomain.ECONOMIC_DATA,
        origin_id="federal_reserve",
        origin_name="Board of Governors of the Federal Reserve System",
    )
    events = []
    for item in root.findall(".//item")[:max_records]:
        title = (item.findtext("title") or "").strip()
        identifier = (item.findtext("guid") or item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        if not title or not identifier or not published:
            continue
        timestamp = parsedate_to_datetime(published)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Federal Reserve feed timestamp has no timezone")
        timestamp = timestamp.astimezone(UTC)
        events.append(
            NormalizedEconomicEvent(
                source=source,
                occurrence_id=f"federal-reserve-document:{identifier}"[:160],
                event_name=title[:240],
                indicator_id="fed_policy_rate",
                country="US",
                currency="USD",
                scheduled_at=timestamp,
                released_at=timestamp,
                release_status=EconomicReleaseStatus.RELEASED,
                expected_impact=EconomicImpact.UNKNOWN,
                release_version=1,
                received_at=retrieved_at,
            )
        )
    return tuple(events)
