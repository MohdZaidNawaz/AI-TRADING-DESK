"""The deterministic Phase 1 F.R.I.D.A.Y. specialist desk."""

from app.agents.base import AnalysisContext, BaseAgent
from app.models.schemas import AgentAnalysis


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
            ],
            limitations=[
                "No live economic releases, central-bank data, rates, or cross-asset prices were used."
            ],
            confidence=0.58,
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
            ],
            limitations=[
                "No positioning, order-flow, holdings, fund-flow, or institutional dataset was accessed."
            ],
            confidence=0.5,
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
            ],
            limitations=[
                "No scanner, news feed, social feed, order flow, watchlist, or market-wide data was accessed."
            ],
            confidence=0.48,
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
            ],
            limitations=["No filings, financial statements, estimates, or valuation data was loaded."],
            confidence=0.5,
        )


class ChartistTechnicianAgent(BaseAgent):
    agent_id = "chartist_technician"
    name = "Chartist Technician"
    expertise = "Price structure, trend, momentum, and key levels"
    routing_keywords = frozenset(
        {"technical", "chart", "trend", "support", "resistance", "momentum", "price", "level"}
    )

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        return self.result(
            summary=f"Built a mock technical review plan for {context.symbol}.",
            findings=[
                "Establish timeframe, trend structure, volatility regime, and nearby invalidation levels.",
                "Require alignment between structure and momentum before treating a setup as actionable research.",
            ],
            limitations=["No chart, candle, volume, indicator, or live price data was analyzed."],
            confidence=0.55,
        )


class OracleQuantAgent(BaseAgent):
    agent_id = "oracle_quant"
    name = "Oracle Quant"
    expertise = "Statistics, probability, models, and scenario analysis"
    routing_keywords = frozenset(
        {"quant", "probability", "model", "statistic", "forecast", "volatility", "correlation"}
    )

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        return self.result(
            summary=f"Defined a mock quantitative validation plan for {context.symbol}.",
            findings=[
                "Measure return distribution, volatility, drawdown, and correlation over an explicit sample window.",
                "Separate in-sample observations from out-of-sample validation before assigning probabilities.",
            ],
            limitations=["No historical dataset, model inference, backtest, or probability forecast was run."],
            confidence=0.52,
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
            ],
            limitations=[
                "No approval gate, account, portfolio, position, broker, order, or execution data is available."
            ],
            confidence=0.7,
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
            ],
            limitations=[
                "Pilot Execution performs no MT5 integration, broker connection, credential handling, order placement, or trade execution."
            ],
            confidence=1.0,
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
            ],
            limitations=[
                "Phase 1 does not persist ledger entries or provide monitoring and system-health responsibilities."
            ],
            confidence=0.65,
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
