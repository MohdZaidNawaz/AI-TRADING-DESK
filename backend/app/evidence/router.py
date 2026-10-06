"""Provider-neutral evidence routing for the nine FRIDAY specialist seats."""

from __future__ import annotations

from dataclasses import replace

from app.evidence.economic_store import latest_economic_versions
from app.evidence.models import (
    CorporateActionEvidence,
    AgentEvidence,
    EvidenceProvenance,
    EvidenceSnapshot,
    FundamentalObservationEvidence,
    FundamentalSeriesEvidence,
    EstimateObservationEvidence,
    EstimateSeriesEvidence,
    PositioningObservationEvidence,
    PositioningSeriesEvidence,
)
from app.models.corporate_actions import CorporateActionType
from app.models.economic_events import EconomicReleaseStatus


NINE_SPECIALIST_IDS: tuple[str, ...] = (
    "atlas_macro",
    "capitol_smart_money",
    "scout_recon",
    "athena_analyst",
    "chartist_technician",
    "oracle_quant",
    "sentinel_risk_officer",
    "pilot_execution",
    "ledger_the_book",
)

_MARKET_AND_NEWS = frozenset(
    {
        "atlas_macro",
        "scout_recon",
        "sentinel_risk_officer",
        "pilot_execution",
        "ledger_the_book",
    }
)
_CONTEXTUAL_MARKET_AND_NEWS = frozenset(
    {"capitol_smart_money", "athena_analyst"}
)
_MARKET_ONLY = frozenset({"chartist_technician", "oracle_quant"})
_CONTEXTUAL_MARKET_LIMIT = 3
_CAPITOL_POSITIONING_OBSERVATION_LIMIT = 100
_ATHENA_FUNDAMENTAL_OBSERVATION_LIMIT = 100
_ATHENA_ESTIMATE_OBSERVATION_LIMIT = 100
_CORPORATE_ACTION_LIMIT = 100
_CHARTIST_CORPORATE_ACTION_LIMIT = 50
_ISSUER_REFERENCE_LIMIT = 100
_PORTFOLIO_RISK_SNAPSHOT_LIMIT = 100
_ATLAS_ECONOMIC_STATUSES = frozenset(
    {
        EconomicReleaseStatus.SCHEDULED,
        EconomicReleaseStatus.RELEASED,
        EconomicReleaseStatus.REVISED,
    }
)
_SCOUT_ECONOMIC_STATUSES = frozenset(
    {
        EconomicReleaseStatus.SCHEDULED,
        EconomicReleaseStatus.RELEASED,
        EconomicReleaseStatus.REVISED,
        EconomicReleaseStatus.DELAYED,
        EconomicReleaseStatus.CANCELLED,
    }
)


class EvidenceRouter:
    """Assign deterministic, provider-neutral evidence subsets to specialists."""

    def route(self, *, agent_id: str, snapshot: EvidenceSnapshot) -> AgentEvidence:
        if agent_id not in NINE_SPECIALIST_IDS:
            raise ValueError(f"Unknown FRIDAY specialist ID: {agent_id}")

        if agent_id in _MARKET_AND_NEWS:
            market_evidence = snapshot.market_evidence
            news_evidence = snapshot.news_evidence
            market_series = ()
        elif agent_id in _CONTEXTUAL_MARKET_AND_NEWS:
            market_evidence = snapshot.market_evidence[:_CONTEXTUAL_MARKET_LIMIT]
            news_evidence = snapshot.news_evidence
            market_series = ()
        elif agent_id in _MARKET_ONLY:
            market_evidence = snapshot.market_evidence
            news_evidence = ()
            market_series = snapshot.market_series
        else:  # pragma: no cover - guarded by NINE_SPECIALIST_IDS above
            raise ValueError(f"No evidence policy exists for specialist: {agent_id}")

        # Macro series are raw, persistent economic observations rather than
        # market bars or discrete economic events.  Atlas alone receives their
        # values in Phase 5B-3; Ledger receives provenance references below.
        macro_series = snapshot.macro_series if agent_id == "atlas_macro" else ()

        # Positioning is a dedicated reported-participation domain. Capitol is
        # the sole consumer of typed values; Ledger receives provenance only.
        # The route cap protects immutable specialist-artifact bounds without
        # changing what the bounded store retains.
        routed_positioning = _bounded_positioning_series(
            snapshot.positioning_series,
            limit=_CAPITOL_POSITIONING_OBSERVATION_LIMIT,
        )
        positioning_series = (
            routed_positioning if agent_id == "capitol_smart_money" else ()
        )

        # Financial-statement evidence is a dedicated issuer-accounting
        # domain.  Athena alone receives its values; Ledger receives only
        # provenance below.  The cap protects specialist-artifact bounds.
        routed_fundamentals = _bounded_fundamental_series(
            snapshot.fundamental_series,
            limit=_ATHENA_FUNDAMENTAL_OBSERVATION_LIMIT,
        )
        fundamental_series = (
            routed_fundamentals if agent_id == "athena_analyst" else ()
        )

        # Source-reported estimates are deliberately distinct from reported
        # financial statements. Athena alone receives typed estimate values;
        # Ledger retains provenance references below.
        routed_estimates = _bounded_estimate_series(
            snapshot.estimate_series,
            limit=_ATHENA_ESTIMATE_OBSERVATION_LIMIT,
        )
        estimate_series = routed_estimates if agent_id == "athena_analyst" else ()

        # Corporate actions are discrete issuer facts, deliberately separate
        # from statements, estimates, and price evidence.  Athena and Scout
        # receive typed facts; Chartist receives only share adjustments for
        # non-economic series-discontinuity context. Ledger has provenance only.
        bounded_corporate_actions = snapshot.corporate_actions[:_CORPORATE_ACTION_LIMIT]
        if agent_id in {"athena_analyst", "scout_recon"}:
            corporate_actions = bounded_corporate_actions
        elif agent_id == "chartist_technician":
            corporate_actions = tuple(
                action
                for action in bounded_corporate_actions
                if action.action_type
                in {CorporateActionType.STOCK_SPLIT, CorporateActionType.REVERSE_STOCK_SPLIT}
            )[:_CHARTIST_CORPORATE_ACTION_LIMIT]
        else:
            corporate_actions = ()

        # Issuer/listing reference profiles are a dedicated source-reported
        # identity and classification domain.  Athena and Scout receive typed
        # factual values; Ledger receives provenance only below.  They are not
        # market evidence and never reach Sentinel or Pilot.
        bounded_issuer_references = snapshot.issuer_references[:_ISSUER_REFERENCE_LIMIT]
        issuer_references = (
            bounded_issuer_references
            if agent_id in {"athena_analyst", "scout_recon"}
            else ()
        )

        # Source-reported account and portfolio context is a dedicated risk
        # domain. Sentinel alone receives typed values; Ledger has provenance
        # only below. It is not market evidence and never reaches Pilot.
        bounded_portfolio_risk = snapshot.portfolio_risk_snapshots[:_PORTFOLIO_RISK_SNAPSHOT_LIMIT]
        portfolio_risk_snapshots = (
            bounded_portfolio_risk if agent_id == "sentinel_risk_officer" else ()
        )

        current_economic_events = latest_economic_versions(snapshot.economic_events)
        if agent_id == "atlas_macro":
            economic_events = tuple(
                event
                for event in current_economic_events
                if event.release_status in _ATLAS_ECONOMIC_STATUSES
            )
        elif agent_id == "scout_recon":
            economic_events = tuple(
                event
                for event in current_economic_events
                if event.release_status in _SCOUT_ECONOMIC_STATUSES
            )
        elif agent_id == "sentinel_risk_officer":
            economic_events = tuple(
                replace(
                    event,
                    previous=None,
                    revised_previous=None,
                    forecast=None,
                    actual=None,
                )
                for event in current_economic_events
            )
        elif agent_id == "ledger_the_book":
            economic_events = snapshot.economic_events
        else:
            economic_events = ()

        provenance: tuple[EvidenceProvenance, ...] = ()
        if agent_id == "ledger_the_book":
            provenance = tuple(
                evidence.provenance for evidence in market_evidence
            ) + tuple(evidence.provenance for evidence in news_evidence) + tuple(
                evidence.provenance for evidence in economic_events
            ) + tuple(
                observation.provenance
                for series in snapshot.macro_series
                for observation in series.observations
            ) + tuple(
                observation.provenance
                for series in routed_positioning
                for observation in series.observations
            ) + tuple(
                observation.provenance
                for series in routed_fundamentals
                for observation in series.observations
            ) + tuple(
                observation.provenance
                for series in routed_estimates
                for observation in series.observations
        ) + tuple(
            action.provenance for action in bounded_corporate_actions
        ) + tuple(
            profile.provenance for profile in bounded_issuer_references
        ) + tuple(
            portfolio.provenance for portfolio in bounded_portfolio_risk
            )

        return AgentEvidence(
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
            portfolio_risk_snapshots=portfolio_risk_snapshots,
            provenance=provenance,
        )


def _bounded_positioning_series(
    series: tuple[PositioningSeriesEvidence, ...],
    *,
    limit: int,
) -> tuple[PositioningSeriesEvidence, ...]:
    """Select recent reported observations deterministically for agent bounds."""
    if limit < 1:  # pragma: no cover - module constant guard
        raise ValueError("positioning routing limit must be positive")

    candidates = sorted(
        (
            (item, observation)
            for item in series
            for observation in item.observations
        ),
        key=lambda item: _positioning_route_key(item[0], item[1]),
    )[:limit]
    selected_ids: dict[str, set[str]] = {}
    for item, observation in candidates:
        selected_ids.setdefault(item.series_id, set()).add(observation.observation_id)
    return tuple(
        item.restricted_to(frozenset(selected_ids[item.series_id]))
        for item in series
        if item.series_id in selected_ids
    )


def _positioning_route_key(
    series: PositioningSeriesEvidence,
    observation: PositioningObservationEvidence,
) -> tuple[int, int, float, str, str]:
    timestamp = (
        observation.published_at
        or observation.observed_at
        or observation.vintage_at
        or observation.received_at
    )
    return (
        -observation.reference_period.end_date.toordinal(),
        -observation.observation_version,
        -timestamp.timestamp(),
        series.series_id,
        observation.observation_id,
    )


def _bounded_fundamental_series(
    series: tuple[FundamentalSeriesEvidence, ...],
    *,
    limit: int,
) -> tuple[FundamentalSeriesEvidence, ...]:
    """Select recent reported observations deterministically for Athena bounds."""
    if limit < 1:  # pragma: no cover - module constant guard
        raise ValueError("fundamental routing limit must be positive")

    candidates = sorted(
        (
            (item, observation)
            for item in series
            for observation in item.observations
        ),
        key=lambda item: _fundamental_route_key(item[0], item[1]),
    )[:limit]
    selected_ids: dict[str, set[str]] = {}
    for item, observation in candidates:
        selected_ids.setdefault(item.series_id, set()).add(observation.observation_id)
    return tuple(
        item.restricted_to(frozenset(selected_ids[item.series_id]))
        for item in series
        if item.series_id in selected_ids
    )


def _fundamental_route_key(
    series: FundamentalSeriesEvidence,
    observation: FundamentalObservationEvidence,
) -> tuple[int, int, float, str, str]:
    timestamp = (
        observation.published_at
        or observation.filed_at
        or observation.reported_at
        or observation.vintage_at
        or observation.received_at
    )
    return (
        -observation.reference_period.end_date.toordinal(),
        -observation.observation_version,
        -timestamp.timestamp(),
        series.series_id,
        observation.observation_id,
    )


def _bounded_estimate_series(
    series: tuple[EstimateSeriesEvidence, ...],
    *,
    limit: int,
) -> tuple[EstimateSeriesEvidence, ...]:
    """Select recent source-reported estimates deterministically for Athena bounds."""
    if limit < 1:  # pragma: no cover - module constant guard
        raise ValueError("estimate routing limit must be positive")

    candidates = sorted(
        (
            (item, observation)
            for item in series
            for observation in item.observations
        ),
        key=lambda item: _estimate_route_key(item[0], item[1]),
    )[:limit]
    selected_ids: dict[str, set[str]] = {}
    for item, observation in candidates:
        selected_ids.setdefault(item.series_id, set()).add(observation.observation_id)
    return tuple(
        item.restricted_to(frozenset(selected_ids[item.series_id]))
        for item in series
        if item.series_id in selected_ids
    )


def _estimate_route_key(
    series: EstimateSeriesEvidence,
    observation: EstimateObservationEvidence,
) -> tuple[int, int, float, str, str]:
    timestamp = observation.published_at or observation.vintage_at or observation.as_of
    return (
        -observation.reference_period.end_date.toordinal(),
        -observation.observation_version,
        -timestamp.timestamp(),
        series.series_id,
        observation.observation_id,
    )
