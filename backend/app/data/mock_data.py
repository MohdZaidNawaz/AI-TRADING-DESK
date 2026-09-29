from app.models.schemas import Agent, Market

AGENTS = [
    Agent(
        id="atlas_macro",
        name="Atlas Macro",
        description="Global regime, rates, and cross-asset context",
        status="active",
    ),
    Agent(
        id="capitol_smart_money",
        name="Capitol Smart Money",
        description="Institutional positioning and capital-flow research",
        status="idle",
    ),
    Agent(
        id="scout_recon",
        name="Scout Recon",
        description="Market surveillance and catalyst reconnaissance",
        status="syncing",
    ),
    Agent(
        id="athena_analyst",
        name="Athena Analyst",
        description="Fundamental context and catalyst analysis",
        status="idle",
    ),
    Agent(
        id="chartist_technician",
        name="Chartist Technician",
        description="Price structure, trend, and momentum",
        status="active",
    ),
    Agent(
        id="oracle_quant",
        name="Oracle Quant",
        description="Statistical models and probability research",
        status="active",
    ),
    Agent(
        id="sentinel_risk_officer",
        name="Sentinel Risk Officer",
        description="Research-risk and exposure oversight",
        status="active",
    ),
    Agent(
        id="pilot_execution",
        name="Pilot Execution",
        description="Disabled future execution coordination",
        status="idle",
    ),
    Agent(
        id="ledger_the_book",
        name="Ledger The Book",
        description="Research memory and decision review",
        status="idle",
    ),
]

MARKETS = [
    Market(
        symbol="XAUUSD",
        name="Gold / US Dollar",
        price=2648.32,
        change_percent=0.42,
        high=2661.00,
        low=2629.00,
    ),
    Market(
        symbol="EURUSD",
        name="Euro / US Dollar",
        price=1.0842,
        change_percent=-0.18,
        high=1.0871,
        low=1.0818,
    ),
    Market(
        symbol="BTCUSD",
        name="Bitcoin / US Dollar",
        price=67284.10,
        change_percent=1.24,
        high=67910.00,
        low=65832.00,
    ),
]
