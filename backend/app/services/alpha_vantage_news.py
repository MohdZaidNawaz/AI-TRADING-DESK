"""Alpha Vantage wiring for provider-neutral XAU/USD news research."""

from datetime import UTC, datetime, timedelta

from app.connectors.alpha_vantage import (
    ALPHA_VANTAGE_NEWS_PAGE_SIZE,
    AlphaVantageNewsClient,
    AlphaVantageNewsConnector,
    alpha_vantage_news_connector,
)
from app.models.events import NormalizedNewsResearchEvent
from app.services.news_research import (
    NewsResearchSort,
    XauUsdNewsRelevanceService,
    xau_usd_news_relevance_service,
)

_XAU_USD_TICKER_FILTER = "FOREX:USD"


class AlphaVantageNewsResearchService:
    """Fetch and normalize Alpha news before shared relevance selection."""

    def __init__(
        self,
        client: AlphaVantageNewsClient,
        connector: AlphaVantageNewsConnector = alpha_vantage_news_connector,
        relevance_service: XauUsdNewsRelevanceService = (
            xau_usd_news_relevance_service
        ),
    ) -> None:
        self._client = client
        self._connector = connector
        self._relevance_service = relevance_service

    @classmethod
    def from_environment(cls) -> "AlphaVantageNewsResearchService":
        return cls(AlphaVantageNewsClient.from_environment())

    async def research_xau_usd(
        self,
        *,
        requested_count: int = 3,
        time_from: datetime | str | None = None,
        time_to: datetime | str | None = None,
        sort: NewsResearchSort | str = "RELEVANCE",
    ) -> tuple[NormalizedNewsResearchEvent, ...]:
        normalized_count = _normalize_requested_count(requested_count)
        normalized_sort = _normalize_research_sort(sort)
        lower_bound, upper_bound = _research_boundaries(time_from, time_to)
        records = await self._client.fetch_news(
            tickers=_XAU_USD_TICKER_FILTER,
            time_from=time_from,
            time_to=time_to,
            sort=normalized_sort,
        )
        events = tuple(self._connector.normalize(record) for record in records)
        return self._relevance_service.select(
            events,
            requested_count=normalized_count,
            sort=normalized_sort,
            starts_at=lower_bound,
            ends_before=upper_bound,
        )


def _normalize_requested_count(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= ALPHA_VANTAGE_NEWS_PAGE_SIZE
    ):
        raise ValueError("requested_count must be between 1 and 50.")
    return value


def _normalize_research_sort(value: NewsResearchSort | str) -> str:
    normalized = str(value).strip().upper()
    if normalized not in {"LATEST", "RELEVANCE"}:
        raise ValueError("research sort must be LATEST or RELEVANCE.")
    return normalized


def _research_boundaries(
    time_from: datetime | str | None,
    time_to: datetime | str | None,
) -> tuple[datetime | None, datetime | None]:
    lower_bound = _parse_research_boundary(time_from, "time_from")
    inclusive_upper_bound = _parse_research_boundary(time_to, "time_to")
    if lower_bound is not None and inclusive_upper_bound is not None:
        if lower_bound > inclusive_upper_bound:
            raise ValueError("time_from must not be after time_to.")

    if inclusive_upper_bound is None:
        return lower_bound, None
    if isinstance(time_to, datetime):
        return lower_bound, inclusive_upper_bound + timedelta(microseconds=1)
    return lower_bound, inclusive_upper_bound + timedelta(minutes=1)


def _parse_research_boundary(
    value: datetime | str | None,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must include a timezone.")
        return value.astimezone(UTC)
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid.")
    normalized = value.strip()
    try:
        return datetime.strptime(normalized, "%Y%m%dT%H%M").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError(
            f"{field_name} must use YYYYMMDDTHHMM format."
        ) from None
