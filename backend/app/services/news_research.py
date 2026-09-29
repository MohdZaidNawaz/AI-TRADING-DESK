"""Provider-neutral relevance selection for normalized news research."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal

from app.models.events import NormalizedNewsResearchEvent

NewsResearchSort = Literal["LATEST", "RELEVANCE"]

_ALLOWED_SORTS = {"LATEST", "RELEVANCE"}
_DIRECT_GOLD_PATTERNS = (
    re.compile(
        r"\bgold\b(?![\s-]+(?:medal(?:s|ist|ists)?|standard)\b)",
        re.IGNORECASE,
    ),
    re.compile(r"\bxau(?:\s*/?\s*usd)?\b", re.IGNORECASE),
    re.compile(r"\bbullion\b", re.IGNORECASE),
    re.compile(r"\bprecious(?:\s+|-)metals?\b", re.IGNORECASE),
)
_GOLD_STANDARD_PATTERN = re.compile(r"\bgold[\s-]+standard\b", re.IGNORECASE)
_FINANCIAL_GOLD_STANDARD_PATTERNS = (
    re.compile(r"\bmonetary\s+(?:policy|system)\b", re.IGNORECASE),
    re.compile(r"\bcurrenc(?:y|ies)\b", re.IGNORECASE),
    re.compile(r"\bcentral\s+banks?\b", re.IGNORECASE),
    re.compile(r"\bgold[-\s]+backed\b", re.IGNORECASE),
    re.compile(r"\bconvertibility\b", re.IGNORECASE),
    re.compile(r"\bbretton\s+woods\b", re.IGNORECASE),
    re.compile(r"\breserve\s+asset\b", re.IGNORECASE),
    re.compile(r"\bfiat\s+(?:money|currenc(?:y|ies))\b", re.IGNORECASE),
)
_MACRO_CONTEXT_PATTERNS = (
    re.compile(r"\bfederal\s+reserve\b", re.IGNORECASE),
    re.compile(r"\bFed\b"),
    re.compile(r"\bfederal\s+funds\s+rate\b", re.IGNORECASE),
    re.compile(r"\binterest\s+rates?\b", re.IGNORECASE),
    re.compile(
        r"\brate\s+(?:decision|decisions|hike|hikes|cut|cuts|pause|pauses|policy|outlook)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!grade\s)(?<!cosmic\s)(?<!balloon\s)(?<!tire\s)(?<!tyre\s)\binflation\b",
        re.IGNORECASE,
    ),
    re.compile(r"\breal\s+(?:yields?|interest\s+rates?)\b", re.IGNORECASE),
    re.compile(r"\busd\b", re.IGNORECASE),
    re.compile(r"\bu\.?s\.?\s+dollar\b", re.IGNORECASE),
    re.compile(r"\bdollar\s+index\b", re.IGNORECASE),
    re.compile(r"\bdxy\b", re.IGNORECASE),
    re.compile(r"\btreasury\s+yields?\b", re.IGNORECASE),
    re.compile(r"\bu\.?s\.?\s+treasur(?:y|ies)\b", re.IGNORECASE),
    re.compile(r"\bgeopolitic(?:al|s)?\b", re.IGNORECASE),
    re.compile(r"\brisk[-\s]+off\b", re.IGNORECASE),
    re.compile(r"\bsafe[-\s]+haven\b", re.IGNORECASE),
    re.compile(
        r"(?<!price\s)(?<!trade\s)(?<!bidding\s)(?<!culture\s)\bwar\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:armed\s+conflict|military\s+escalation)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:economic|financial|international|trade)\s+sanctions?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bsanctions?\s+(?:against|on)\b",
        re.IGNORECASE,
    ),
)
_DIRECT_GOLD_SYMBOLS = frozenset({"XAU", "XAU/USD", "XAUUSD", "GLD", "IAU"})
_MACRO_CONTEXT_SYMBOLS = frozenset({"USD", "DXY", "USDX"})


@dataclass(frozen=True)
class _ScoredNewsEvent:
    event: NormalizedNewsResearchEvent
    tier: int
    local_score: int
    provider_relevance: float


class XauUsdNewsRelevanceService:
    """Filter and rank normalized XAU/USD news without provider assumptions."""

    def select(
        self,
        events: Sequence[NormalizedNewsResearchEvent],
        *,
        requested_count: int,
        sort: NewsResearchSort | str,
        starts_at: datetime | None = None,
        ends_before: datetime | None = None,
    ) -> tuple[NormalizedNewsResearchEvent, ...]:
        if (
            isinstance(requested_count, bool)
            or not isinstance(requested_count, int)
            or requested_count < 1
        ):
            raise ValueError("requested_count must be a positive integer.")

        normalized_sort = str(sort).strip().upper()
        if normalized_sort not in _ALLOWED_SORTS:
            raise ValueError("research sort must be LATEST or RELEVANCE.")
        _validate_time_window(starts_at, ends_before)

        deduplicated: dict[str, _ScoredNewsEvent] = {}
        key = _latest_key if normalized_sort == "LATEST" else _relevance_key
        for event in events:
            if starts_at is not None and event.timestamp < starts_at:
                continue
            if ends_before is not None and event.timestamp >= ends_before:
                continue

            candidate = _score_event(event)
            if candidate is None:
                continue
            existing = deduplicated.get(event.event_id)
            if existing is None or key(candidate) < key(existing):
                deduplicated[event.event_id] = candidate

        candidates = sorted(deduplicated.values(), key=key)
        return tuple(candidate.event for candidate in candidates[:requested_count])


xau_usd_news_relevance_service = XauUsdNewsRelevanceService()


def _validate_time_window(
    starts_at: datetime | None,
    ends_before: datetime | None,
) -> None:
    for value, field_name in (
        (starts_at, "starts_at"),
        (ends_before, "ends_before"),
    ):
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError(f"{field_name} must include a timezone.")
    if starts_at is not None and ends_before is not None:
        if starts_at >= ends_before:
            raise ValueError("time_from must not be after time_to.")


def _score_event(event: NormalizedNewsResearchEvent) -> _ScoredNewsEvent | None:
    direct_score = _weighted_text_score(event, _DIRECT_GOLD_PATTERNS, direct=True)
    direct_score += _financial_gold_standard_score(event)
    context_score = _weighted_text_score(event, _MACRO_CONTEXT_PATTERNS)
    direct_symbols = _matching_symbols(event, _DIRECT_GOLD_SYMBOLS)
    context_symbols = _matching_symbols(event, _MACRO_CONTEXT_SYMBOLS)

    is_direct = direct_score > 0 or bool(direct_symbols)
    is_context = context_score > 0 or bool(context_symbols)
    if not is_direct and not is_context:
        return None

    metadata_boost = _metadata_boost(event, direct=is_direct, context=is_context)
    symbol_score = 10 * len(direct_symbols) + 5 * len(context_symbols)
    local_score = direct_score + context_score + symbol_score + metadata_boost
    return _ScoredNewsEvent(
        event=event,
        tier=2 if is_direct else 1,
        local_score=local_score,
        provider_relevance=_provider_relevance(event),
    )


def _financial_gold_standard_score(event: NormalizedNewsResearchEvent) -> int:
    searchable_text = " ".join(
        filter(
            None,
            (event.title, event.summary, *event.topics, event.category),
        )
    )
    if _GOLD_STANDARD_PATTERN.search(searchable_text) is None:
        return 0
    if _pattern_hits(searchable_text, _FINANCIAL_GOLD_STANDARD_PATTERNS) == 0:
        return 0

    weights = (12, 6, 9, 5)
    score = weights[0] * _pattern_hits(event.title, (_GOLD_STANDARD_PATTERN,))
    score += weights[1] * _pattern_hits(event.summary, (_GOLD_STANDARD_PATTERN,))
    score += sum(
        weights[2] * _pattern_hits(topic, (_GOLD_STANDARD_PATTERN,))
        for topic in event.topics
    )
    score += weights[3] * _pattern_hits(
        event.category,
        (_GOLD_STANDARD_PATTERN,),
    )
    return score


def _weighted_text_score(
    event: NormalizedNewsResearchEvent,
    patterns: Sequence[re.Pattern[str]],
    *,
    direct: bool = False,
) -> int:
    weights = (12, 6, 9, 5) if direct else (6, 3, 4, 2)
    score = weights[0] * _pattern_hits(event.title, patterns)
    score += weights[1] * _pattern_hits(event.summary, patterns)
    score += sum(weights[2] * _pattern_hits(topic, patterns) for topic in event.topics)
    score += weights[3] * _pattern_hits(event.category, patterns)
    return score


def _metadata_boost(
    event: NormalizedNewsResearchEvent,
    *,
    direct: bool,
    context: bool,
) -> int:
    score = 0
    for value in (event.provider, event.publisher_domain):
        if direct:
            score += _pattern_hits(value, _DIRECT_GOLD_PATTERNS)
        if context:
            score += _pattern_hits(value, _MACRO_CONTEXT_PATTERNS)
    return score


def _matching_symbols(
    event: NormalizedNewsResearchEvent,
    relevant_symbols: frozenset[str],
) -> tuple[str, ...]:
    return tuple(
        symbol for symbol in event.symbols if symbol.upper() in relevant_symbols
    )


def _provider_relevance(event: NormalizedNewsResearchEvent) -> float:
    topic_scores = [
        item.relevance_score
        for item in event.topic_relevance
        if _pattern_hits(item.topic, _DIRECT_GOLD_PATTERNS)
        or _pattern_hits(item.topic, _MACRO_CONTEXT_PATTERNS)
    ]
    symbol_scores = [
        item.relevance_score
        for item in event.symbol_sentiment
        if item.symbol in _DIRECT_GOLD_SYMBOLS
        or item.symbol in _MACRO_CONTEXT_SYMBOLS
    ]
    return max([0.0, *topic_scores, *symbol_scores])


def _pattern_hits(
    value: str | None,
    patterns: Sequence[re.Pattern[str]],
) -> int:
    if value is None:
        return 0
    return sum(pattern.search(value) is not None for pattern in patterns)


def _relevance_key(
    candidate: _ScoredNewsEvent,
) -> tuple[float, float, float, float, str, tuple[str, ...]]:
    return (
        -float(candidate.tier),
        -float(candidate.local_score),
        -candidate.provider_relevance,
        -candidate.event.timestamp.timestamp(),
        candidate.event.event_id,
        _stable_event_key(candidate.event),
    )


def _latest_key(
    candidate: _ScoredNewsEvent,
) -> tuple[float, float, float, float, str, tuple[str, ...]]:
    return (
        -candidate.event.timestamp.timestamp(),
        -float(candidate.tier),
        -float(candidate.local_score),
        -candidate.provider_relevance,
        candidate.event.event_id,
        _stable_event_key(candidate.event),
    )


def _stable_event_key(event: NormalizedNewsResearchEvent) -> tuple[str, ...]:
    return (
        str(event.url),
        event.title,
        event.provider,
        event.summary or "",
    )
