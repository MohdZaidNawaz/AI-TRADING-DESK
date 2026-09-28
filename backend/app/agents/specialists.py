from app.agents.base import AnalysisContext, BaseAgent
from app.models.schemas import AgentAnalysis


class MacroAgent(BaseAgent):
    agent_id = "macro"
    name = "Macro"
    expertise = "Global regimes, rates, liquidity, and cross-asset context"
    routing_keywords = frozenset({"macro", "economy", "inflation", "rate", "fed", "yield", "dollar", "liquidity"})

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        return self.result(
            summary=f"Prepared a mock macro framework for {context.symbol}.",
            findings=[
                "Review rate, currency, liquidity, and risk-regime sensitivity.",
                "Confirm whether the requested horizon is tactical or structural before weighting macro factors.",
            ],
            limitations=["No live economic releases, central-bank data, rates, or cross-asset prices were used."],
            confidence=0.58,
        )


class NewsAgent(BaseAgent):
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


class TechnicalAgent(BaseAgent):
    agent_id = "technical"
    name = "Technical"
    expertise = "Price structure, trend, momentum, and key levels"
    routing_keywords = frozenset({"technical", "chart", "trend", "support", "resistance", "momentum", "price", "level"})

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


class QuantAgent(BaseAgent):
    agent_id = "quant"
    name = "Quant"
    expertise = "Statistics, probability, models, and scenario analysis"
    routing_keywords = frozenset({"quant", "probability", "model", "statistic", "forecast", "volatility", "correlation"})

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


class FundamentalAgent(BaseAgent):
    agent_id = "fundamental"
    name = "Fundamental"
    expertise = "Valuation, financial quality, and fundamental catalysts"
    routing_keywords = frozenset({"fundamental", "valuation", "earnings", "revenue", "balance", "cash flow", "value"})

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


class RiskAgent(BaseAgent):
    agent_id = "risk"
    name = "Risk"
    expertise = "Exposure, invalidation, uncertainty, and risk controls"
    routing_keywords = frozenset({"risk", "exposure", "drawdown", "stop", "position", "loss", "invalid"})

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        return self.result(
            summary=f"Applied a mock research-risk review to {context.symbol}.",
            findings=[
                "Define invalidation conditions and maximum acceptable research uncertainty before any decision.",
                "Do not infer position sizing or execution parameters without verified prices and portfolio context.",
            ],
            limitations=["No account, portfolio, position, broker, order, or execution data is available."],
            confidence=0.7,
        )


class ReconAgent(BaseAgent):
    agent_id = "recon"
    name = "Recon"
    expertise = "Market surveillance, anomalies, and contextual reconnaissance"
    routing_keywords = frozenset({"recon", "scan", "sentiment", "unusual", "watchlist", "surveillance", "market"})

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        return self.result(
            summary=f"Outlined a mock reconnaissance sweep for {context.symbol}.",
            findings=[
                "Check relative activity, cross-market confirmation, sentiment shifts, and unusual behavior.",
                "Escalate anomalies only after confirming them across independent, time-aligned data sources.",
            ],
            limitations=["No scanner, social feed, order flow, watchlist, or market-wide data was accessed."],
            confidence=0.48,
        )


class JournalAgent(BaseAgent):
    agent_id = "journal"
    name = "Journal"
    expertise = "Research memory, audit trail, and decision review"
    routing_keywords = frozenset({"journal", "history", "review", "record", "memory", "decision"})

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        query_excerpt = context.query[:120]
        return self.result(
            summary=f"Created a mock journal entry template for {context.symbol}.",
            findings=[
                f'Record the original request: "{query_excerpt}".',
                "Capture assumptions, consulted agents, invalidation criteria, and the eventual outcome for review.",
            ],
            limitations=["The first version does not persist journal entries to a database."],
            confidence=0.65,
        )


def create_specialist_agents() -> list[BaseAgent]:
    return [
        MacroAgent(),
        NewsAgent(),
        TechnicalAgent(),
        QuantAgent(),
        FundamentalAgent(),
        RiskAgent(),
        ReconAgent(),
        JournalAgent(),
    ]
