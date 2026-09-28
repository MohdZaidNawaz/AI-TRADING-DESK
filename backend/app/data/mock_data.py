from app.models.schemas import Agent, Market

AGENTS = [
    Agent(id="macro", name="Macro", description="Global regime analysis", status="active"),
    Agent(id="news", name="News", description="News and event intelligence", status="syncing"),
    Agent(id="technical", name="Technical", description="Price structure and momentum", status="active"),
    Agent(id="quant", name="Quant", description="Statistical models and probability", status="active"),
    Agent(id="fundamental", name="Fundamental", description="Value and catalyst analysis", status="idle"),
    Agent(id="risk", name="Risk", description="Risk and exposure oversight", status="active"),
    Agent(id="recon", name="Recon", description="Market surveillance", status="syncing"),
    Agent(id="journal", name="Journal", description="Desk intelligence memory", status="idle"),
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
