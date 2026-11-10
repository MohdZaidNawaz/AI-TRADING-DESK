"""The deterministic Phase 1 F.R.I.D.A.Y. specialist desk."""

from datetime import datetime

from app.agents.base import AnalysisContext, BaseAgent
from app.models.events import EventType
from app.models.derived_features import FeatureStatus
from app.models.specialist_analysis import (
    SpecialistAnalysis,
    SpecialistCorporateActionObservation,
    SpecialistDerivedObservation,
    SpecialistEconomicObservation,
    SpecialistFactObservation,
    SpecialistFundamentalObservation,
    SpecialistIssuerReferenceObservation,
    SpecialistEstimateObservation,
    SpecialistInterpretation,
    SpecialistMacroObservation,
    SpecialistOperationalState,
    SpecialistPortfolioContextState,
    SpecialistPortfolioRiskObservation,
    SpecialistPortfolioRiskMetricObservation,
    SpecialistPortfolioRiskMetricsState,
    SpecialistPositioningObservation,
    SpecialistStance,
)
from app.models.schemas import AgentAnalysis
from app.models.technical_analysis import (
    TechnicalCalculationStatus,
    TrendDirection,
)


def _market_context_facts(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistFactObservation, ...]:
    """Describe assigned market evidence without inferring a market signal."""
    return tuple(
        SpecialistFactObservation(
            statement=(
                f'Observed normalized market event "{evidence.event}" for '
                f"{evidence.symbol or 'an unspecified symbol'} at {evidence.timestamp.isoformat()}."
            ),
            evidence_references=(agent.market_reference(evidence),),
        )
        for evidence in context.evidence.market_evidence
    )


def _news_context_facts(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistFactObservation, ...]:
    """Retain normalized news titles and timestamps without article content."""
    return tuple(
        SpecialistFactObservation(
            statement=(
                f'Observed normalized news context "{evidence.title[:500]}" at '
                f"{evidence.provenance.event_timestamp.isoformat()}."
            ),
            evidence_references=(agent.news_reference(evidence, symbol=context.symbol),),
        )
        for evidence in context.evidence.news_evidence
    )


def _economic_observations(
    agent: BaseAgent,
    context: AnalysisContext,
    *,
    include_values: bool = True,
) -> tuple[SpecialistEconomicObservation, ...]:
    """Preserve typed releases without calculating surprises or direction."""
    return tuple(
        SpecialistEconomicObservation(
            event_id=evidence.event_id,
            occurrence_id=evidence.occurrence_id,
            event_name=evidence.event_name,
            indicator_id=evidence.indicator_id,
            country=evidence.country,
            region=evidence.region,
            currency=evidence.currency,
            reference_period=evidence.reference_period,
            scheduled_at=evidence.scheduled_at,
            released_at=evidence.released_at,
            release_status=evidence.release_status,
            expected_impact=evidence.expected_impact,
            previous=evidence.previous if include_values else None,
            revised_previous=evidence.revised_previous if include_values else None,
            forecast=evidence.forecast if include_values else None,
            actual=evidence.actual if include_values else None,
            release_version=evidence.release_version,
            revision_of_event_id=evidence.revision_of_event_id,
            evidence_reference=agent.economic_reference(evidence),
        )
        for evidence in context.evidence.economic_events
    )


def _macro_observations(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistMacroObservation, ...]:
    """Preserve raw macro values and vintages without deriving a macro signal."""
    return tuple(
        SpecialistMacroObservation(
            observation_id=observation.observation_id,
            series_id=series.series_id,
            series_key=series.key,
            reference_period=observation.reference_period,
            value=observation.value,
            observation_version=observation.observation_version,
            revision_of_observation_id=observation.revision_of_observation_id,
            observed_at=observation.observed_at,
            effective_at=observation.effective_at,
            published_at=observation.published_at,
            vintage_at=observation.vintage_at,
            received_at=observation.received_at,
            evidence_reference=agent.macro_reference(observation),
        )
        for series in context.evidence.macro_series
        for observation in series.observations
    )


def _positioning_observations(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistPositioningObservation, ...]:
    """Preserve reported values without inferring intent, exposure, or direction."""
    return tuple(
        SpecialistPositioningObservation(
            observation_id=observation.observation_id,
            series_id=series.series_id,
            series_key=series.key,
            reference_period=observation.reference_period,
            value=observation.value,
            observation_version=observation.observation_version,
            revision_of_observation_id=observation.revision_of_observation_id,
            observed_at=observation.observed_at,
            published_at=observation.published_at,
            vintage_at=observation.vintage_at,
            received_at=observation.received_at,
            evidence_reference=agent.positioning_reference(observation),
        )
        for series in context.evidence.positioning_series
        for observation in series.observations
    )


def _fundamental_observations(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistFundamentalObservation, ...]:
    """Preserve typed reported financial values without valuation inference."""
    return tuple(
        SpecialistFundamentalObservation(
            observation_id=evidence.observation_id,
            series_id=evidence.series_id,
            series_key=series.key,
            reference_period=evidence.reference_period,
            value=evidence.value,
            observation_version=evidence.observation_version,
            revision_of_observation_id=evidence.revision_of_observation_id,
            reported_at=evidence.reported_at,
            filed_at=evidence.filed_at,
            published_at=evidence.published_at,
            vintage_at=evidence.vintage_at,
            received_at=evidence.received_at,
            evidence_reference=agent.fundamental_reference(evidence),
        )
        for series in context.evidence.fundamental_series
        for evidence in series.observations
    )


def _estimate_observations(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistEstimateObservation, ...]:
    """Preserve source-reported estimates without valuation or forecast inference."""
    return tuple(
        SpecialistEstimateObservation(
            observation_id=evidence.observation_id,
            series_id=evidence.series_id,
            series_key=series.key,
            reference_period=evidence.reference_period,
            value=evidence.value,
            as_of=evidence.as_of,
            observation_version=evidence.observation_version,
            revision_of_observation_id=evidence.revision_of_observation_id,
            published_at=evidence.published_at,
            vintage_at=evidence.vintage_at,
            received_at=evidence.received_at,
            evidence_reference=agent.estimate_reference(evidence),
        )
        for series in context.evidence.estimate_series
        for evidence in series.observations
    )


def _corporate_action_observations(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistCorporateActionObservation, ...]:
    """Retain assigned issuer actions without price adjustment or inference."""
    return tuple(
        SpecialistCorporateActionObservation(
            action_id=evidence.action_id,
            instrument=evidence.instrument,
            occurrence_id=evidence.occurrence_id,
            action_type=evidence.action_type,
            lifecycle=evidence.lifecycle,
            cash_dividend_terms=evidence.cash_dividend_terms,
            share_adjustment_terms=evidence.share_adjustment_terms,
            published_at=evidence.published_at,
            effective_at=evidence.effective_at,
            ex_date=evidence.ex_date,
            record_date=evidence.record_date,
            payment_date=evidence.payment_date,
            action_version=evidence.action_version,
            revision_of_action_id=evidence.revision_of_action_id,
            evidence_reference=agent.corporate_action_reference(evidence),
        )
        for evidence in context.evidence.corporate_actions
    )


def _issuer_reference_observations(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistIssuerReferenceObservation, ...]:
    """Preserve source-reported identity and taxonomy without inference."""
    return tuple(
        SpecialistIssuerReferenceObservation(
            profile_id=evidence.profile_id,
            instrument=evidence.instrument,
            source_profile_id=evidence.source_profile_id,
            issuer_display_name=evidence.issuer_display_name,
            issuer_legal_name=evidence.issuer_legal_name,
            instrument_display_name=evidence.instrument_display_name,
            asset_class=evidence.asset_class,
            instrument_kind=evidence.instrument_kind,
            status=evidence.status,
            classification=evidence.classification,
            country=evidence.country,
            domicile_country=evidence.domicile_country,
            reference_currency=evidence.reference_currency,
            as_of=evidence.as_of,
            published_at=evidence.published_at,
            profile_version=evidence.profile_version,
            revision_of_profile_id=evidence.revision_of_profile_id,
            evidence_reference=agent.issuer_reference_reference(evidence),
        )
        for evidence in context.evidence.issuer_references
    )


def _portfolio_risk_observations(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistPortfolioRiskObservation, ...]:
    """Preserve reported account context without calculating portfolio risk."""
    return tuple(
        SpecialistPortfolioRiskObservation(
            snapshot_id=evidence.snapshot_id,
            account=evidence.account,
            snapshot_occurrence_id=evidence.snapshot_occurrence_id,
            account_state=evidence.account_state,
            positions=evidence.positions,
            risk_limits=evidence.risk_limits,
            as_of=evidence.as_of,
            published_at=evidence.published_at,
            snapshot_version=evidence.snapshot_version,
            revision_of_snapshot_id=evidence.revision_of_snapshot_id,
            evidence_reference=agent.portfolio_risk_reference(evidence),
        )
        for evidence in context.evidence.portfolio_risk_snapshots
    )


def _portfolio_risk_metric_observations(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistPortfolioRiskMetricObservation, ...]:
    """Retain deterministic risk facts without applying a risk policy."""
    return tuple(
        SpecialistPortfolioRiskMetricObservation(
            metric_id=metric.metric_id,
            metric_type=metric.metric_type,
            status=metric.status,
            value=metric.value,
            unit=metric.unit,
            currency=metric.currency,
            calculation_version=metric.calculation_version,
            as_of=metric.as_of,
            calculated_at=metric.calculated_at,
            input_references=metric.input_references,
            evidence_reference=agent.portfolio_risk_metrics_reference(evidence),
        )
        for evidence in context.evidence.portfolio_risk_metric_snapshots
        for metric in evidence.metrics
    )


def _technical_facts(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistFactObservation, ...]:
    facts: list[SpecialistFactObservation] = []
    for evidence in context.evidence.market_evidence:
        values = tuple(
            (label, value)
            for label, value in (
                ("price", evidence.price),
                ("open", evidence.open),
                ("high", evidence.high),
                ("low", evidence.low),
                ("close", evidence.close),
                ("volume", evidence.volume),
            )
            if value is not None
        )
        if not values and evidence.timeframe is None:
            continue
        rendered_values = ", ".join(f"{label}={value}" for label, value in values)
        timeframe = evidence.timeframe or "not supplied"
        facts.append(
            SpecialistFactObservation(
                statement=(
                    f"Observed OHLC/volume fields ({rendered_values or 'none supplied'}) "
                    f"on timeframe {timeframe}."
                ),
                evidence_references=(agent.market_reference(evidence),),
            )
        )
    return tuple(facts)


def _quantitative_facts(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistFactObservation, ...]:
    facts: list[SpecialistFactObservation] = []
    for evidence in context.evidence.market_evidence:
        values = tuple(
            (label, value)
            for label, value in (
                ("price", evidence.price),
                ("open", evidence.open),
                ("high", evidence.high),
                ("low", evidence.low),
                ("close", evidence.close),
                ("volume", evidence.volume),
            )
            if value is not None
        ) + tuple((f"indicator.{name}", value) for name, value in evidence.indicator_values)
        if not values:
            continue
        rendered_values = ", ".join(f"{label}={value}" for label, value in values)
        facts.append(
            SpecialistFactObservation(
                statement=f"Observed finite numeric market values: {rendered_values}.",
                evidence_references=(agent.market_reference(evidence),),
            )
        )
    return tuple(facts)


def _technical_series_facts(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistFactObservation, ...]:
    """Describe validated bars and continuity without deriving indicators."""
    facts: list[SpecialistFactObservation] = []
    for series in context.evidence.market_series:
        latest = series.latest_completed_bar
        latest_values = (
            (
                f"latest completed OHLC open={latest.open}, high={latest.high}, "
                f"low={latest.low}, close={latest.close}; volume={latest.volume} "
                f"({latest.volume_kind.value})"
            )
            if latest is not None
            else "no completed bar is present"
        )
        facts.append(
            SpecialistFactObservation(
                statement=(
                    f"Observed {series.bar_count} validated {series.timeframe} bar(s) for "
                    f"{series.symbol}: completed={series.completed_count}, "
                    f"provisional={series.provisional_count}, "
                    f"oldest_completed={_render_time(series.oldest_completed_at)}, "
                    f"latest_completed={_render_time(series.latest_completed_at)}, "
                    f"missing_intervals={series.continuity.missing_interval_count}; "
                    f"{latest_values}."
                ),
                evidence_references=(agent.series_reference(series),),
            )
        )
    return tuple(facts)


def _quantitative_series_facts(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistFactObservation, ...]:
    """Expose direct finite bar values without statistical calculations."""
    facts: list[SpecialistFactObservation] = []
    for series in context.evidence.market_series:
        latest = series.latest_completed_bar
        values = (
            (
                f"open={latest.open}, high={latest.high}, low={latest.low}, "
                f"close={latest.close}, volume={latest.volume}"
            )
            if latest is not None
            else "no completed OHLCV values"
        )
        facts.append(
            SpecialistFactObservation(
                statement=(
                    f"Observed direct finite values from {series.bar_count} ordered "
                    f"{series.timeframe} bar(s): {values}; completed={series.completed_count}, "
                    f"provisional={series.provisional_count}, "
                    f"missing_intervals={series.continuity.missing_interval_count}."
                ),
                evidence_references=(agent.series_reference(series),),
            )
        )
    return tuple(facts)


def _render_time(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "none"


def _derived_feature_observations(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistDerivedObservation, ...]:
    """Describe assigned calculations without recomputing or interpreting them."""
    observations: list[SpecialistDerivedObservation] = []
    for feature in context.derived_features:
        parameters = ", ".join(
            f"{parameter.name}={parameter.value}" for parameter in feature.parameters
        )
        parameter_text = f" ({parameters})" if parameters else ""
        value_text = (
            f"value={feature.value} {feature.value_unit.value}"
            if feature.status is FeatureStatus.AVAILABLE
            else "no value"
        )
        quality_text = ",".join(flag.value for flag in feature.quality_flags) or "none"
        observations.append(
            SpecialistDerivedObservation(
                statement=(
                    f"Derived {feature.feature_type.value}{parameter_text}: {value_text}; "
                    f"status={feature.status.value}; inputs={feature.input_bar_count}/"
                    f"{feature.required_bar_count}; method={feature.method_id}@"
                    f"{feature.method_version}; quality={quality_text}."
                ),
                feature_references=(agent.derived_feature_reference(feature),),
            )
        )
    return tuple(observations)


def _fundamental_facts(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistFactObservation, ...]:
    return tuple(
        SpecialistFactObservation(
            statement=(
                f'Observed normalized fundamental-update event "{evidence.event}" '
                f"at {evidence.timestamp.isoformat()}."
            ),
            evidence_references=(agent.market_reference(evidence),),
        )
        for evidence in context.evidence.market_evidence
        if evidence.event_type is EventType.FUNDAMENTAL_UPDATE
    )


def _ledger_facts(
    agent: BaseAgent,
    context: AnalysisContext,
) -> tuple[SpecialistFactObservation, ...]:
    facts: list[SpecialistFactObservation] = []
    for evidence in context.evidence.market_evidence:
        facts.append(
            SpecialistFactObservation(
                statement=(
                    f'Current-run provenance retains normalized evidence event "{evidence.provenance.event_id}" '
                    f"at {evidence.provenance.event_timestamp.isoformat()}."
                ),
                evidence_references=(agent.market_reference(evidence),),
            )
        )
    for evidence in context.evidence.news_evidence:
        facts.append(
            SpecialistFactObservation(
                statement=(
                    f'Current-run provenance retains normalized evidence event "{evidence.provenance.event_id}" '
                    f"at {evidence.provenance.event_timestamp.isoformat()}."
                ),
                evidence_references=(agent.news_reference(evidence, symbol=context.symbol),),
            )
        )
    return tuple(facts)


class AtlasMacroAgent(BaseAgent):
    agent_id = "atlas_macro"
    name = "Atlas Macro"
    expertise = "Global regimes, rates, liquidity, and cross-asset context"
    routing_keywords = frozenset(
        {"macro", "economy", "inflation", "rate", "fed", "yield", "dollar", "liquidity"}
    )

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        macro_report = context.macro_intelligence
        if macro_report is not None and macro_report.instrument_assessments:
            assessment = macro_report.instrument_assessments[0]
            calculated = [
                f"{item.indicator_id}: {item.direction.value} "
                f"({item.previous_period_end} to {item.current_period_end})."
                for item in macro_report.indicator_changes
                if item.status.value == "available" and item.direction is not None
            ]
            return self.result(
                summary=(
                    f"Reviewed {len(assessment.verified_facts)} validated cached macro fact(s), "
                    f"{len(calculated)} period change(s), and "
                    f"{len(assessment.conflicting_evidence_ids)} conflict reference(s) for "
                    f"{context.symbol}; no provider request was made."
                ),
                findings=[
                    *assessment.verified_facts[:8],
                    *calculated[:6],
                    *assessment.conditional_implications[:4],
                    *self.evidence_findings(context),
                ],
                limitations=[
                    *assessment.missing_inputs,
                    *assessment.warnings,
                    *assessment.limitations,
                    *macro_report.warnings,
                    "Observed macro facts do not establish price causation or direction.",
                ],
                confidence=assessment.confidence,
            )
        intelligence = context.news_intelligence
        if intelligence is not None and intelligence.macro_findings:
            findings = [
                f"Verified: {item.verified_fact} Interpretation: {item.interpretation} "
                f"Possible Gold implication: {item.possible_gold_implication}"
                for item in intelligence.macro_findings[:6]
            ]
            return self.result(
                summary=(
                    f"Interpreted {len(intelligence.macro_findings)} evidence-linked macro finding(s) "
                    f"for {context.symbol}; provider status={intelligence.news_retrieval.status.value}, "
                    f"cache={intelligence.news_retrieval.cache_status.value}."
                ),
                findings=[*findings, *self.evidence_findings(context)],
                limitations=[
                    *intelligence.missing_information,
                    *intelligence.warnings,
                    "Interpretations are deterministic and do not establish price causation or direction.",
                ],
                confidence=0.66,
            )
        return self.result(
            summary=(
                f"No evidence-linked macro finding is available for {context.symbol}; "
                f"provider status={intelligence.news_retrieval.status.value}, "
                f"cache={intelligence.news_retrieval.cache_status.value}."
                if intelligence is not None
                else f"Prepared a mock macro framework for {context.symbol}."
            ),
            findings=[
                "Review rate, currency, liquidity, and risk-regime sensitivity.",
                "Confirm whether the requested horizon is tactical or structural before weighting macro factors.",
                *self.evidence_findings(context),
            ],
            limitations=[
                "No live economic releases, central-bank data, rates, or cross-asset prices were used.",
                *(intelligence.missing_information if intelligence is not None else ()),
                *(intelligence.warnings if intelligence is not None else ()),
            ],
            confidence=0.58,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                "Assigned normalized market and news context does not establish a verified macro "
                "regime or directional macro conclusion."
            ),
            fact_observations=_market_context_facts(self, context)
            + _news_context_facts(self, context),
            economic_observations=_economic_observations(self, context),
            macro_observations=_macro_observations(self, context),
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "Verified economic releases, rates, liquidity, and cross-asset inputs are "
                        "required before a macro assessment can be made."
                    )
                ),
            ),
            invalidating_conditions=(
                "No macro conclusion is valid without verified macroeconomic and cross-asset evidence.",
            ),
        )


class CapitolSmartMoneyAgent(BaseAgent):
    agent_id = "capitol_smart_money"
    name = "Capitol Smart Money"
    expertise = "Institutional positioning, capital flows, and participation context"
    routing_keywords = frozenset(
        {
            "smart money",
            "institutional",
            "positioning",
            "flow",
            "capital",
            "commitment",
            "cot",
            "liquidity",
            "order block",
            "fair value gap",
            "fvg",
            "bos",
            "choch",
            "market structure",
            "strategy",
            "scenario",
            "entry",
            "target",
        }
    )

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        liquidity = context.liquidity_intelligence
        strategy = context.risk_strategy_intelligence
        if liquidity is not None and liquidity.analyses:
            levels = sum(len(item.liquidity_levels) for item in liquidity.analyses)
            sweeps = sum(len(item.sweeps) for item in liquidity.analyses)
            gaps = sum(len(item.fair_value_gaps) for item in liquidity.analyses)
            breaks = sum(len(item.structure_breaks) for item in liquidity.analyses)
            blocks = sum(len(item.order_block_candidates) for item in liquidity.analyses)
            return self.result(
                summary=(
                    f"Calculated deterministic price-action liquidity intelligence for "
                    f"{context.symbol} across {len(liquidity.analyses)} validated timeframe(s)."
                ),
                findings=[
                    (
                        f"{item.timeframe}: structure={item.structure_trend.value}; "
                        f"levels={len(item.liquidity_levels)}, sweeps={len(item.sweeps)}, "
                        f"FVGs={len(item.fair_value_gaps)}, breaks={len(item.structure_breaks)}, "
                        f"order-block candidates={len(item.order_block_candidates)}; "
                        f"freshness={item.freshness}; provider={item.provider_id}."
                    )
                    for item in liquidity.analyses
                ]
                + [
                    f"Aggregate candidates: {levels} liquidity level(s), {sweeps} sweep(s), "
                    f"{gaps} FVG(s), {breaks} BOS/CHoCH event(s), and {blocks} order-block candidate(s).",
                    f"Multi-timeframe alignment: {liquidity.alignment.state.value}.",
                    *(
                        f"{item.direction.value} scenario={item.status.value}; "
                        f"quality={item.quality.score:.1f}/100 ({item.quality.sufficiency.value}); "
                        f"risk/reward={item.risk_reward.ratio if item.risk_reward.ratio is not None else 'unavailable'}."
                        for item in (() if strategy is None else strategy.scenarios)
                    ),
                    *self.evidence_findings(context),
                ],
                limitations=[
                    *(
                        limitation
                        for item in liquidity.analyses
                        for limitation in item.limitations
                    ),
                    "Price-action candidates do not prove resting institutional orders, intent, or positioning.",
                    "No automatic trade signal or execution instruction was produced.",
                    *(() if strategy is None else strategy.limitations),
                ],
                confidence=(
                    0.65
                    if all(item.freshness == "fresh" for item in liquidity.analyses)
                    else 0.4
                ),
            )
        has_positioning = bool(context.evidence.positioning_series)
        return self.result(
            summary=f"Prepared a mock institutional-participation framework for {context.symbol}.",
            findings=[
                "Define which positioning, flow, and participation measures would be relevant to the research question.",
                "Separate confirmed positioning evidence from narrative claims about institutional activity.",
                *self.evidence_findings(context),
            ],
            limitations=[
                (
                    "Reported positioning evidence is descriptive only; no institutional intent, "
                    "net exposure, signal, probability, or trade direction was inferred."
                    if has_positioning
                    else "No positioning, order-flow, holdings, fund-flow, or institutional dataset was accessed."
                )
            ],
            confidence=0.5,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                (
                    "Deterministic OHLCV-derived structure and liquidity candidates are retained as "
                    "price-action calculations only; they do not establish institutional orders, "
                    "intent, or a directional conclusion."
                    if context.liquidity_intelligence is not None
                    else "Reported positioning observations are retained as factual context only and do "
                    "not establish institutional intent, net exposure, or a directional conclusion."
                    if context.evidence.positioning_series
                    else "No normalized positioning, participation, holdings, or capital-flow evidence is "
                    "available for a smart-money assessment."
                )
            ),
            positioning_observations=_positioning_observations(self, context),
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "OHLCV-derived liquidity candidates must not be converted into observed order flow, "
                        "institutional intent, probability, trade direction, or trade parameters."
                        if context.liquidity_intelligence is not None
                        else "Reported positioning values must not be converted into institutional intent, "
                        "net exposure, probability, trade direction, or trade parameters."
                        if context.evidence.positioning_series
                        else "Market and news context alone must not be treated as evidence of institutional "
                        "positioning or flow."
                    )
                ),
            ),
            invalidating_conditions=(
                "A smart-money conclusion requires verified positioning or flow evidence.",
            ),
        )


class ScoutReconAgent(BaseAgent):
    agent_id = "scout_recon"
    name = "Scout Recon"
    expertise = "Market surveillance, anomalies, catalysts, and contextual reconnaissance"
    routing_keywords = frozenset(
        {
            "recon",
            "scan",
            "sentiment",
            "unusual",
            "watchlist",
            "surveillance",
            "market",
            "news",
            "headline",
            "event",
            "catalyst",
            "announcement",
            "calendar",
        }
    )

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        intelligence = context.news_intelligence
        if intelligence is not None and intelligence.relevant_news:
            newly_retrieved = sum(
                item.evidence_origin.value == "newly_retrieved"
                for item in intelligence.relevant_news
            )
            cached = len(intelligence.relevant_news) - newly_retrieved
            return self.result(
                summary=(
                    f"Reviewed {len(intelligence.relevant_news)} validated relevant news item(s) "
                    f"for {context.symbol} ({newly_retrieved} newly retrieved, {cached} cached); "
                    f"provider status={intelligence.news_retrieval.status.value}."
                ),
                findings=[
                    f'{item.provider_name}: "{item.headline}" '
                    f"({item.event_at.isoformat()}, {item.freshness.value}, "
                    f"{item.evidence_origin.value}); "
                    f"evidence={item.evidence_id}."
                    for item in intelligence.relevant_news
                ] + self.evidence_findings(context),
                limitations=[
                    *intelligence.missing_information,
                    *intelligence.warnings,
                    "Headline relevance does not prove that an article caused a price move.",
                ],
                confidence=0.68,
            )
        return self.result(
            summary=(
                f"No validated relevant news item is available for {context.symbol}; "
                f"provider status={intelligence.news_retrieval.status.value}, "
                f"cache={intelligence.news_retrieval.cache_status.value}."
                if intelligence is not None
                else f"Outlined a mock reconnaissance sweep for {context.symbol}."
            ),
            findings=[
                "Check relative activity, cross-market confirmation, event context, and unusual behavior.",
                "Escalate anomalies only after confirming them across independent, time-aligned data sources.",
                *self.evidence_findings(context),
            ],
            limitations=[
                "No scanner, news feed, social feed, order flow, watchlist, or market-wide data was accessed.",
                *(intelligence.missing_information if intelligence is not None else ()),
                *(intelligence.warnings if intelligence is not None else ()),
            ],
            confidence=0.48,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        corporate_actions = _corporate_action_observations(self, context)
        issuer_references = _issuer_reference_observations(self, context)
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                "Assigned market and news context is retained for reconnaissance, but it does not "
                "establish a verified anomaly or directional conclusion."
            ),
            fact_observations=_market_context_facts(self, context)
            + _news_context_facts(self, context),
            economic_observations=_economic_observations(self, context),
            corporate_action_observations=corporate_actions,
            issuer_reference_observations=issuer_references,
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "Catalyst or anomaly assessment requires independent, time-aligned confirmation."
                    )
                ),
            ),
            invalidating_conditions=(
                "Unconfirmed market or news context must not be escalated as an anomaly.",
            ),
        )


class AthenaAnalystAgent(BaseAgent):
    agent_id = "athena_analyst"
    name = "Athena Analyst"
    expertise = "Valuation, financial quality, and fundamental catalysts"
    routing_keywords = frozenset(
        {"fundamental", "valuation", "earnings", "revenue", "balance", "cash flow", "value"}
    )

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        has_fundamentals = bool(context.evidence.fundamental_series)
        has_estimates = bool(context.evidence.estimate_series)
        return self.result(
            summary=f"Prepared a mock fundamental diligence framework for {context.symbol}.",
            findings=[
                "Identify the symbol's relevant value drivers, financial inputs, and comparison set.",
                "Distinguish durable fundamental changes from short-lived narrative catalysts.",
                *self.evidence_findings(context),
            ],
            limitations=[
                (
                    "Reported financial-statement observations and source-reported estimates are factual "
                    "context only; no valuation, ratio, FRIDAY forecast, probability, or trade conclusion "
                    "was inferred."
                    if has_fundamentals or has_estimates
                    else "No filings, financial statements, estimates, or valuation data was loaded."
                )
            ],
            confidence=0.5,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        facts = _fundamental_facts(self, context)
        fundamentals = _fundamental_observations(self, context)
        estimates = _estimate_observations(self, context)
        corporate_actions = _corporate_action_observations(self, context)
        issuer_references = _issuer_reference_observations(self, context)
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                (
                    "Reported financial-statement observations are retained as factual context only and "
                    "do not establish valuation, financial quality, a forecast, or a directional conclusion."
                    if fundamentals or estimates or corporate_actions or issuer_references
                    else "No verified valuation, financial-statement, estimate, or comparable-company data is "
                    "available for a fundamental conclusion."
                )
            ),
            fact_observations=facts,
            fundamental_observations=fundamentals,
            estimate_observations=estimates,
            corporate_action_observations=corporate_actions,
            issuer_reference_observations=issuer_references,
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "Reported accounting values and source-reported estimate values preserve distinct "
                        "target-period, unit, vintage, and revision context without calculating valuation "
                        "ratios, financial ratios, FRIDAY forecasts, probabilities, trade direction, or "
                        "trade parameters."
                    ),
                    evidence_references=tuple(
                        reference
                        for observation in fundamentals
                        for reference in (observation.evidence_reference,)
                    ) + tuple(
                        reference
                        for observation in estimates
                        for reference in (observation.evidence_reference,)
                    ) + tuple(
                        reference
                        for observation in corporate_actions
                        for reference in (observation.evidence_reference,)
                    ) + tuple(
                        reference
                        for observation in issuer_references
                        for reference in (observation.evidence_reference,)
                    ),
                )
                if fundamentals or estimates or corporate_actions or issuer_references
                else SpecialistInterpretation(
                    statement=(
                        "A normalized fundamental-update event can be recorded, but it is insufficient "
                        "on its own for valuation or financial-quality analysis."
                    ),
                    evidence_references=tuple(
                        reference
                        for fact in facts
                        for reference in fact.evidence_references
                    ),
                )
                if facts
                else SpecialistInterpretation(
                    statement=(
                        "Fundamental analysis remains unavailable until verified fundamental data is assigned."
                    )
                ),
            ),
            invalidating_conditions=(
                "Fundamental conclusions require verified financial and valuation evidence.",
            ),
        )


class ChartistTechnicianAgent(BaseAgent):
    agent_id = "chartist_technician"
    name = "Chartist Technician"
    expertise = "Price structure, trend, momentum, and key levels"
    routing_keywords = frozenset(
        {"technical", "chart", "trend", "support", "resistance", "momentum", "price", "level"}
    )

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        report = context.technical_analysis
        analyses = report.analyses if report is not None else ()
        if not analyses:
            return self.result(
                summary=f"Technical analysis is unavailable for {context.symbol}.",
                findings=[*self.evidence_findings(context)],
                limitations=[
                    "No validated historical OHLCV series was available for deterministic calculation."
                ],
                confidence=0.0,
            )
        findings: list[str] = []
        limitations: list[str] = []
        for analysis in analyses:
            available = sum(
                feature.status is FeatureStatus.AVAILABLE
                for feature in analysis.indicators
            )
            findings.append(
                f"{analysis.timeframe}: trend={analysis.trend_direction.value}; "
                f"indicators={available}/{len(analysis.indicators)} available; "
                f"completed candles={analysis.completed_candle_count}; "
                f"provisional excluded={analysis.provisional_candle_count}."
            )
            if analysis.structure_observations:
                findings.append(
                    f"{analysis.timeframe} structure: "
                    + ", ".join(analysis.structure_observations)
                    + "."
                )
            if analysis.support_candidates:
                findings.append(
                    f"{analysis.timeframe} mathematical support candidates: "
                    + ", ".join(f"{level.price:g}" for level in analysis.support_candidates)
                    + "."
                )
            if analysis.resistance_candidates:
                findings.append(
                    f"{analysis.timeframe} mathematical resistance candidates: "
                    + ", ".join(f"{level.price:g}" for level in analysis.resistance_candidates)
                    + "."
                )
            limitations.extend(analysis.limitations)
        limitations.append(
            "Calculated levels are mathematical swing candidates, not institutional significance or trade instructions."
        )
        confidence = 0.72 if all(
            analysis.status is TechnicalCalculationStatus.AVAILABLE for analysis in analyses
        ) else 0.48
        return self.result(
            summary=f"Calculated deterministic technical observations for {context.symbol}.",
            findings=[*findings, *self.evidence_findings(context)],
            limitations=list(dict.fromkeys(limitations)),
            confidence=confidence,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        facts = _technical_facts(self, context) + _technical_series_facts(self, context)
        corporate_actions = _corporate_action_observations(self, context)
        report = context.technical_analysis
        trends = {
            analysis.trend_direction
            for analysis in (report.analyses if report is not None else ())
        }
        if trends == {TrendDirection.BULLISH}:
            stance = SpecialistStance.BULLISH
        elif trends == {TrendDirection.BEARISH}:
            stance = SpecialistStance.BEARISH
        elif not trends or trends == {TrendDirection.INSUFFICIENT_DATA}:
            stance = SpecialistStance.INSUFFICIENT_DATA
        elif TrendDirection.INSUFFICIENT_DATA in trends:
            stance = SpecialistStance.CONFLICTED
        else:
            stance = SpecialistStance.NEUTRAL
        thesis = (
            "; ".join(
                f"{analysis.timeframe} deterministic trend={analysis.trend_direction.value}"
                for analysis in report.analyses
            )
            if report is not None and report.analyses
            else "No validated historical series was available for technical calculation."
        )
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=stance,
            thesis=thesis,
            fact_observations=facts,
            derived_observations=_derived_feature_observations(self, context),
            corporate_action_observations=corporate_actions,
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        (
                            "Deterministic indicator and structure calculations are separated from "
                            "their analytical interpretation and retain their source lineage."
                        )
                        if context.evidence.market_series
                        else (
                            "Observed OHLC, volume, timeframe, and indicator values are recorded without "
                            "deriving unsupported technical signals."
                        )
                    ),
                    evidence_references=tuple(
                        reference
                        for fact in facts
                        for reference in fact.evidence_references
                    ),
                )
                if facts
                else SpecialistInterpretation(
                    statement="No market OHLC, volume, timeframe, or indicator evidence was assigned."
                ),
            ),
            invalidating_conditions=(
                "Technical observations are invalidated if their source series or deterministic calculation lineage changes.",
            ),
        )


class OracleQuantAgent(BaseAgent):
    agent_id = "oracle_quant"
    name = "Oracle Quant"
    expertise = "Statistics, probability, models, and scenario analysis"
    routing_keywords = frozenset(
        {"quant", "probability", "model", "statistic", "forecast", "volatility", "correlation"}
    )

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        limitations = (
            [
                "Deterministic return/statistical features remain descriptive; no inference, forecast, backtest, probability, or direction was produced."
            ]
            if context.derived_features
            else [
                "Historical bars remain descriptive; no model inference, statistic, backtest, return, volatility, or probability forecast was run."
            ]
            if context.evidence.market_series
            else ["No historical dataset, model inference, backtest, or probability forecast was run."]
        )
        return self.result(
            summary=f"Defined a mock quantitative validation plan for {context.symbol}.",
            findings=[
                "Measure return distribution, volatility, drawdown, and correlation over an explicit sample window.",
                "Separate in-sample observations from out-of-sample validation before assigning probabilities.",
                *self.evidence_findings(context),
            ],
            limitations=limitations,
            confidence=0.52,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        facts = _quantitative_facts(self, context) + _quantitative_series_facts(self, context)
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                (
                    "Finite numerical observations and validated bars are descriptive evidence only and "
                    "do not support a statistic, forecast, probability, model result, or direction."
                )
                if context.evidence.market_series
                else (
                    "Finite numerical observations are available only as isolated evidence and do not "
                    "support a statistical forecast, probability, model result, or directional conclusion."
                )
            ),
            fact_observations=facts,
            derived_observations=_derived_feature_observations(self, context),
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        (
                            "Calculated returns or dispersion remain descriptive values with explicit "
                            "lineage; no model, inference, forecast, or probability was applied."
                        )
                        if context.derived_features
                        else (
                            "No historical sample, model specification, validation window, or backtest "
                            "was assigned to the quantitative specialist."
                        )
                    )
                ),
            ),
            invalidating_conditions=(
                "Quantitative conclusions require an explicit validated dataset and methodology.",
            ),
        )


class SentinelRiskOfficerAgent(BaseAgent):
    agent_id = "sentinel_risk_officer"
    name = "Sentinel Risk Officer"
    expertise = "Exposure, invalidation, uncertainty, and research-risk controls"
    routing_keywords = frozenset({"risk", "exposure", "drawdown", "stop", "position", "loss", "invalid"})

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        risk_strategy = context.risk_strategy_intelligence
        intelligence = context.news_intelligence
        if risk_strategy is not None:
            scenario_findings = [
                (
                    f"{item.direction.value}: status={item.status.value}, "
                    f"stop={item.stop_loss_candidate if item.stop_loss_candidate is not None else 'unavailable'}, "
                    f"risk/reward={item.risk_reward.ratio if item.risk_reward.ratio is not None else 'unavailable'}, "
                    f"sizing={item.position_sizing.status.value}, quality={item.quality.score:.1f}/100."
                )
                for item in risk_strategy.scenarios
            ]
            return self.result(
                summary=(
                    f"Completed deterministic non-authorizing risk research for {context.symbol}; "
                    f"report status={risk_strategy.status.value}, volatility={risk_strategy.volatility.regime.value}."
                ),
                findings=[
                    *scenario_findings,
                    *(
                        f"Verified event risk: {item.event_name} at {item.scheduled_at.isoformat()}."
                        for item in (() if intelligence is None else intelligence.event_risks)
                    ),
                    *self.evidence_findings(context),
                ],
                limitations=[
                    *risk_strategy.missing_inputs,
                    *risk_strategy.warnings,
                    *risk_strategy.limitations,
                    "Position sizing remains unavailable unless verified account and contract inputs are explicitly supplied.",
                ],
                confidence=(0.72 if risk_strategy.status.value in {"available", "partial"} else 0.45),
            )
        if intelligence is not None and intelligence.event_risks:
            return self.result(
                summary=f"Identified {len(intelligence.event_risks)} verified upcoming event-risk item(s) for {context.symbol}.",
                findings=[
                    f"{item.event_name} at {item.scheduled_at.isoformat()} ({item.impact.value} impact): {item.risk_statement}"
                    for item in intelligence.event_risks
                ] + self.evidence_findings(context),
                limitations=[
                    "Event risk is informational and creates no authorization, position sizing, or execution authority.",
                    *intelligence.missing_information,
                    *intelligence.warnings,
                ],
                confidence=0.7,
            )
        return self.result(
            summary=(
                f"Applied a non-authorizing research-risk review to {context.symbol}; "
                f"news provider status={intelligence.news_retrieval.status.value}, "
                f"cache={intelligence.news_retrieval.cache_status.value}."
                if intelligence is not None
                else f"Applied a mock research-risk review to {context.symbol}."
            ),
            findings=[
                "Define invalidation conditions and maximum acceptable research uncertainty before any decision.",
                "Do not infer position sizing or execution parameters without verified prices and portfolio context.",
                *self.evidence_findings(context),
            ],
            limitations=[
                "No approval gate, account, portfolio, position, broker, order, or execution data is available.",
                *(intelligence.missing_information if intelligence is not None else ()),
                *(intelligence.warnings if intelligence is not None else ()),
            ],
            confidence=0.7,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        facts = _market_context_facts(self, context) + _news_context_facts(self, context)
        portfolio_risk = _portfolio_risk_observations(self, context)
        portfolio_risk_metrics = _portfolio_risk_metric_observations(self, context)
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                "Assigned market and news context can inform preliminary risk review, but no final "
                "approval, rejection, or portfolio-risk conclusion is produced here."
            ),
            fact_observations=facts,
            economic_observations=_economic_observations(
                self,
                context,
                include_values=False,
            ),
            portfolio_risk_observations=portfolio_risk,
            portfolio_context_state=(
                SpecialistPortfolioContextState.PORTFOLIO_CONTEXT_AVAILABLE
                if portfolio_risk
                else SpecialistPortfolioContextState.PORTFOLIO_CONTEXT_INSUFFICIENT
            ),
            portfolio_risk_metric_observations=portfolio_risk_metrics,
            portfolio_risk_metrics_state=(
                SpecialistPortfolioRiskMetricsState.PORTFOLIO_RISK_METRICS_AVAILABLE
                if any(item.status.value == "available" for item in portfolio_risk_metrics)
                else SpecialistPortfolioRiskMetricsState.PORTFOLIO_RISK_METRICS_INSUFFICIENT
            ),
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "Reported portfolio context and deterministic derived metrics are factual only; "
                        "they create no threshold policy, sizing, approval, or execution decision."
                    )
                ),
            ),
            invalidating_conditions=(
                "Only the Sentinel risk gate may issue a final approval or rejection.",
            ),
        )


class PilotExecutionAgent(BaseAgent):
    """Phase 1 placeholder for a future execution coordination capability."""

    agent_id = "pilot_execution"
    name = "Pilot Execution"
    expertise = "Disabled future execution coordination"
    routing_keywords = frozenset({"execution", "execute", "order", "broker", "mt5", "trade"})

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        return self.result(
            summary=f"Pilot Execution is disabled and not authorized for {context.symbol}.",
            findings=[
                "Phase 1 records that future execution coordination would require an approved proposal and explicit adapter policy.",
                *self.evidence_findings(context),
            ],
            limitations=[
                "Pilot Execution performs no MT5 integration, broker connection, credential handling, order placement, or trade execution."
            ],
            confidence=1.0,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis="Pilot Execution is disabled and has no execution authority.",
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "Read-only evidence context cannot authorize execution, broker access, order "
                        "placement, or adapter use."
                    )
                ),
            ),
            invalidating_conditions=(
                "Pilot remains disabled regardless of specialist stance or evidence content.",
            ),
            operational_state=SpecialistOperationalState.DISABLED_NO_EXECUTION,
        )


class LedgerTheBookAgent(BaseAgent):
    agent_id = "ledger_the_book"
    name = "Ledger The Book"
    expertise = "Research memory, audit trail, and decision review"
    routing_keywords = frozenset({"journal", "history", "review", "record", "memory", "decision"})

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        query_excerpt = context.query[:120]
        return self.result(
            summary=f"Created a mock ledger entry template for {context.symbol}.",
            findings=[
                f'Record the original request: "{query_excerpt}".',
                "Capture assumptions, consulted agents, invalidation criteria, and the eventual outcome for review.",
                *self.evidence_findings(context),
            ],
            limitations=[
                "Phase 1 does not persist ledger entries or provide monitoring and system-health responsibilities."
            ],
            confidence=0.65,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                "Ledger retains current-run provenance and lifecycle metadata only; it has no persistent "
                "historical memory in this phase."
            ),
            fact_observations=_ledger_facts(self, context),
            economic_observations=_economic_observations(self, context),
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "Prior specialist-result metadata describes this one lifecycle and is not a "
                        "persisted decision history."
                    )
                ),
            ),
            invalidating_conditions=(
                "No persistent ledger or monitoring conclusion may be inferred from current-run metadata.",
            ),
        )


class NewsAgent(BaseAgent):
    """Legacy shared-intelligence capability, intentionally not a specialist seat."""

    agent_id = "news"
    name = "News"
    expertise = "News, events, and catalyst intelligence"
    routing_keywords = frozenset({"news", "headline", "event", "catalyst", "announcement", "calendar"})

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        return self.result(
            summary=f"Created a mock event-risk checklist for {context.symbol}.",
            findings=[
                "Check scheduled economic, company, and regulatory events relevant to the symbol.",
                "Validate publication time, source quality, and whether an event is already reflected in price.",
                *self.evidence_findings(context),
            ],
            limitations=["No news feed, web search, filings, or event calendar was queried."],
            confidence=0.5,
        )


# Preserve existing specialist imports while the public desk now uses canonical IDs.
MacroAgent = AtlasMacroAgent
ReconAgent = ScoutReconAgent
FundamentalAgent = AthenaAnalystAgent
TechnicalAgent = ChartistTechnicianAgent
QuantAgent = OracleQuantAgent
RiskAgent = SentinelRiskOfficerAgent
JournalAgent = LedgerTheBookAgent


def create_specialist_agents() -> list[BaseAgent]:
    """Return the exact nine specialist seats; F.R.I.D.A.Y. is the master, not a seat."""
    return [
        AtlasMacroAgent(),
        CapitolSmartMoneyAgent(),
        ScoutReconAgent(),
        AthenaAnalystAgent(),
        ChartistTechnicianAgent(),
        OracleQuantAgent(),
        SentinelRiskOfficerAgent(),
        PilotExecutionAgent(),
        LedgerTheBookAgent(),
    ]
