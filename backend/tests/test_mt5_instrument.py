from types import SimpleNamespace

from app.execution.mt5_adapter import MT5ExecutionAdapter, MT5Settings
from tests.test_mt5_adapter import FakeMT5, _settings


def test_symbol_selection_can_make_an_existing_symbol_visible() -> None:
    fake = FakeMT5(); fake.info.visible = False
    adapter = MT5ExecutionAdapter(settings=_settings(), mt5_module=fake, symbol_mappings={"XAUUSD": "GOLD"})
    contract, codes = adapter.resolve_instrument("XAUUSD")
    assert codes == () and contract is not None and contract.visible


def test_positions_and_order_status_are_read_only_and_deterministic() -> None:
    fake = FakeMT5()
    fake.positions_get = lambda: (
        SimpleNamespace(ticket=2, symbol="XAUUSD.a", volume=0.2, price_open=100.0, sl=95.0, tp=110.0, profit=1.0, magic=7, comment="one"),
        SimpleNamespace(ticket=1, symbol="XAUUSD.a", volume=0.1, price_open=99.0, sl=94.0, tp=109.0, profit=2.0, magic=7, comment="two"),
    )
    fake.orders_get = lambda **_kwargs: (SimpleNamespace(ticket=10, symbol="XAUUSD.a", volume_initial=1.0, volume_current=1.0, price_open=100.0, sl=95.0, tp=110.0, state="placed", magic=7, comment="safe"),)
    adapter = MT5ExecutionAdapter(settings=_settings(), mt5_module=fake, symbol_mappings={"XAUUSD": "XAUUSD.a"})
    assert tuple(item.ticket for item in adapter.positions()) == (1, 2)
    assert adapter.order_status(10).ticket == 10
    assert fake.order_send_calls == 0


def test_market_closed_is_reported_without_symbol_inference() -> None:
    fake = FakeMT5(); fake.tick = None
    adapter = MT5ExecutionAdapter(settings=_settings(), mt5_module=fake, symbol_mappings={"XAUUSD": "XAUUSD.a"})
    _contract, codes = adapter.resolve_instrument("XAUUSD")
    assert codes == ("MT5_MARKET_CLOSED",)
