"""The deterministic Phase 1 F.R.I.D.A.Y. specialist desk."""

from datetime import datetime

from app.agents.base import AnalysisContext, BaseAgent
from app.models.events import EventType
from app.models.specialist_analysis import (
    SpecialistAnalysis,
    SpecialistFactObservation,
    SpecialistInterpretation,
    SpecialistOperationalState,
    SpecialistStance,
)
from app.models.schemas import AgentAnalysis


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
                f'Observed normalized news context "{evidence.title[:500]}" '
                f"published at {evidence.published_at.isoformat()}."
            ),
            evidence_references=(agent.news_reference(evidence, symbol=context.symbol),),
        )
        for evidence in context.evidence.news_evidence
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
        return self.result(
            summary=f"Prepared a mock macro framework for {context.symbol}.",
            findings=[
                "Review rate, currency, liquidity, and risk-regime sensitivity.",
                "Confirm whether the requested horizon is tactical or structural before weighting macro factors.",
                *self.evidence_findings(context),
            ],
            limitations=[
                "No live economic releases, central-bank data, rates, or cross-asset prices were used."
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
        {"smart money", "institutional", "positioning", "flow", "capital", "commitment", "cot"}
    )

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        return self.result(
            summary=f"Prepared a mock institutional-participation framework for {context.symbol}.",
            findings=[
                "Define which positioning, flow, and participation measures would be relevant to the research question.",
                "Separate confirmed positioning evidence from narrative claims about institutional activity.",
                *self.evidence_findings(context),
            ],
            limitations=[
                "No positioning, order-flow, holdings, fund-flow, or institutional dataset was accessed."
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
                "No normalized positioning, participation, holdings, or capital-flow evidence is "
                "available for a smart-money assessment."
            ),
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "Market and news context alone must not be treated as evidence of institutional "
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
        return self.result(
            summary=f"Outlined a mock reconnaissance sweep for {context.symbol}.",
            findings=[
                "Check relative activity, cross-market confirmation, event context, and unusual behavior.",
                "Escalate anomalies only after confirming them across independent, time-aligned data sources.",
                *self.evidence_findings(context),
            ],
            limitations=[
                "No scanner, news feed, social feed, order flow, watchlist, or market-wide data was accessed."
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
        return self.result(
            summary=f"Prepared a mock fundamental diligence framework for {context.symbol}.",
            findings=[
                "Identify the symbol's relevant value drivers, financial inputs, and comparison set.",
                "Distinguish durable fundamental changes from short-lived narrative catalysts.",
                *self.evidence_findings(context),
            ],
            limitations=["No filings, financial statements, estimates, or valuation data was loaded."],
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
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                "No verified valuation, financial-statement, estimate, or comparable-company data is "
                "available for a fundamental conclusion."
            ),
            fact_observations=facts,
            interpretations=(
                SpecialistInterpretation(
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
        limitations = (
            [
                "Historical bars remain descriptive; no indicator, level, signal, or directional methodology was applied."
            ]
            if context.evidence.market_series
            else ["No chart, candle, volume, indicator, or live price data was analyzed."]
        )
        return self.result(
            summary=f"Built a mock technical review plan for {context.symbol}.",
            findings=[
                "Establish timeframe, trend structure, volatility regime, and nearby invalidation levels.",
                "Require alignment between structure and momentum before treating a setup as actionable research.",
                *self.evidence_findings(context),
            ],
            limitations=limitations,
            confidence=0.55,
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
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                (
                    "Assigned market observations and validated bars do not by themselves establish "
                    "trend, momentum, levels, or a directional technical conclusion."
                )
                if context.evidence.market_series
                else (
                    "Assigned market observations do not provide a validated historical sample for trend, "
                    "momentum, support, resistance, or a directional technical conclusion."
                )
            ),
            fact_observations=facts,
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        (
                            "Observed OHLC, volume, timeframe, and continuity values are recorded "
                            "without deriving unsupported technical signals."
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
                "Technical conclusions require a validated time-series and an explicit methodology.",
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
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "No historical sample, model specification, validation window, or backtest was "
                        "assigned to the quantitative specialist."
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
        return self.result(
            summary=f"Applied a mock research-risk review to {context.symbol}.",
            findings=[
                "Define invalidation conditions and maximum acceptable research uncertainty before any decision.",
                "Do not infer position sizing or execution parameters without verified prices and portfolio context.",
                *self.evidence_findings(context),
            ],
            limitations=[
                "No approval gate, account, portfolio, position, broker, order, or execution data is available."
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
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "Portfolio, exposure, proposal parameters, explicit risk policy, and required "
                        "risk assessments remain outside this specialist artifact."
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
