from types import SimpleNamespace

import pytest

from app import config
from app.execution.mt5_adapter import MT5ExecutionAdapter, MT5Settings
from app.models.execution import ExecutionStatus
from app.models.mt5 import MT5ExecutionMode, MT5SessionState
from app.models.portfolio_risk import SourceAccountKey
from tests.test_execution import _intent


class FakeMT5:
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_ORDER_MARKET = 1
    SYMBOL_TRADE_EXECUTION_REQUEST = 0
    SYMBOL_TRADE_EXECUTION_INSTANT = 1
    SYMBOL_TRADE_EXECUTION_MARKET = 2
    SYMBOL_TRADE_EXECUTION_EXCHANGE = 3
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_FILLING_BOC = 3
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1

    def __init__(self, *, initialized=True, authorized=True) -> None:
        self.initialized = initialized
        self.authorized = authorized
        self.order_send_calls = 0
        self.info = SimpleNamespace(
            visible=True, digits=2, point=0.01, trade_tick_size=0.01,
            trade_tick_value=1.0, trade_contract_size=100.0, volume_min=0.1,
            volume_max=10.0, volume_step=0.1, trade_mode=1, filling_mode=1,
            order_mode=1, trade_exemode=self.SYMBOL_TRADE_EXECUTION_MARKET,
        )
        self.tick = SimpleNamespace(bid=100.0, ask=100.1)
        self._account = SimpleNamespace(login=123, server="Broker", balance=1000.0,
            equity=1010.0, margin=100.0, margin_free=910.0, currency="USD", trade_allowed=True)
        self._terminal = SimpleNamespace(trade_allowed=True)

    def initialize(self, **_kwargs): return self.initialized
    def login(self, _login, **_kwargs): return self.authorized
    def account_info(self): return self._account
    def terminal_info(self): return self._terminal
    def version(self): return (5, 0, 6180)
    def symbol_info(self, _symbol): return self.info
    def symbol_select(self, _symbol, _selected): self.info.visible = True; return True
    def symbol_info_tick(self, _symbol): return self.tick
    def positions_get(self): return ()
    def orders_get(self, **_kwargs): return ()
    def order_send(self, _request): self.order_send_calls += 1; raise AssertionError("must not be called")


def _settings(**changes):
    data = {"login": 123, "password": "test-password", "server": "Broker"}
    data.update(changes)
    return MT5Settings(**data)


def _adapter(fake=None, **changes):
    return MT5ExecutionAdapter(
        settings=_settings(), mt5_module=fake or FakeMT5(),
        symbol_mappings={"XAUUSD": "XAUUSD.a"}, **changes,
    )


def _intent_for_mt5(**changes):
    account_id = SourceAccountKey(source_id="mt5", source_account_id="123", account_reference="Broker").account_id
    values = {"account_id": account_id, "quantity": 1.0, "order_type": "MARKET", "filling_mode": "FOK"}
    values.update(changes)
    return _intent(**values)


def test_missing_configuration_fails_closed_without_loading_terminal() -> None:
    adapter = MT5ExecutionAdapter(settings=MT5Settings(login=None), mt5_module=FakeMT5())
    assert adapter.availability() == "MT5_NOT_CONFIGURED"
    assert adapter.session_state.value == "unavailable"


def test_mt5_configuration_uses_process_values_before_local_env(monkeypatch) -> None:
    monkeypatch.setattr(
        config,
        "dotenv_values",
        lambda _path: {"MT5_LOGIN": "2", "MT5_PASSWORD": "local-fixture", "MT5_SERVER": "Local"},
    )
    monkeypatch.setenv("MT5_LOGIN", "1")
    settings = config.get_mt5_settings()
    assert settings["MT5_LOGIN"] == "1"
    assert settings["MT5_PASSWORD"] == "local-fixture"
    assert "local-fixture" not in repr(MT5Settings.from_environment())


def test_default_mode_is_disabled_and_live_is_hard_blocked() -> None:
    assert _adapter().execution_mode.value == "disabled"
    live = _adapter(execution_mode=MT5ExecutionMode.LIVE)
    assert "MT5_EXECUTION_DISABLED" in live.prepare(_intent_for_mt5()).reason_codes


def test_package_unavailable_is_a_safe_error(monkeypatch) -> None:
    adapter = MT5ExecutionAdapter(settings=_settings(), symbol_mappings={"XAUUSD": "XAUUSD"})
    monkeypatch.setattr("app.execution.mt5_adapter.importlib.import_module", lambda _name: (_ for _ in ()).throw(ModuleNotFoundError()))
    assert adapter.availability() == "MT5_NOT_INSTALLED"


@pytest.mark.parametrize(
    ("initialized", "authorized", "expected"),
    [(False, True, "MT5_NOT_INITIALIZED"), (True, False, "MT5_AUTHORIZATION_FAILED")],
)
def test_initialization_and_authorization_fail_closed(initialized, authorized, expected) -> None:
    assert _adapter(FakeMT5(initialized=initialized, authorized=authorized)).availability() == expected


def test_mocked_connection_exposes_safe_read_only_account_identity() -> None:
    account, codes = _adapter().account_information()
    assert codes == ()
    assert account is not None
    assert account.account.source_id == "mt5"
    assert account.account.source_account_id == "123"
    assert account.balance == 1000.0
    assert account.terminal_version == "5.0.6180"


def test_settings_and_results_never_expose_password() -> None:
    settings = _settings(password="never-print-this")
    assert "never-print-this" not in repr(settings)
    result = _adapter().prepare(_intent_for_mt5())
    assert "test-password" not in repr(result)
    assert "test-password" not in str(result.safe_request)


@pytest.mark.parametrize(
    ("mappings", "expected"),
    [({}, "MT5_SYMBOL_MAPPING_MISSING"), ({"XAUUSD": ("GOLD", "XAUUSD.a")}, "MT5_SYMBOL_MAPPING_AMBIGUOUS")],
)
def test_symbol_mapping_is_explicit_and_fails_closed(mappings, expected) -> None:
    contract, codes = MT5ExecutionAdapter(settings=_settings(), mt5_module=FakeMT5(), symbol_mappings=mappings).resolve_instrument("XAUUSD")
    assert contract is None
    assert codes == (expected,)


def test_symbol_not_found_or_not_visible_or_not_tradable_is_rejected() -> None:
    missing = FakeMT5(); missing.symbol_info = lambda _symbol: None
    assert _adapter(missing).resolve_instrument("XAUUSD")[1] == ("MT5_SYMBOL_NOT_FOUND",)
    hidden = FakeMT5(); hidden.info.visible = False; hidden.symbol_select = lambda *_args: False
    assert _adapter(hidden).resolve_instrument("XAUUSD")[1] == ("MT5_SYMBOL_NOT_VISIBLE",)
    disabled = FakeMT5(); disabled.info.trade_mode = disabled.SYMBOL_TRADE_MODE_DISABLED
    assert _adapter(disabled).resolve_instrument("XAUUSD")[1] == ("MT5_SYMBOL_NOT_TRADABLE",)


def test_instrument_contract_preserves_canonical_and_broker_symbols() -> None:
    contract, codes = _adapter().resolve_instrument("XAUUSD")
    assert codes == () and contract is not None
    assert contract.canonical_symbol == "XAUUSD"
    assert contract.broker_symbol == "XAUUSD.a"
    assert contract.minimum_volume == 0.1
    assert contract.filling_modes == ("FOK",)


@pytest.mark.parametrize(
    ("filling_mode", "expected"),
    [
        (1, ("FOK",)),
        (2, ("IOC",)),
        (4, ("BOOK_OR_CANCEL",)),
        (1 | 2, ("FOK", "IOC")),
        (2 | 4, ("IOC", "BOOK_OR_CANCEL")),
        (1 | 2 | 4, ("FOK", "IOC", "BOOK_OR_CANCEL")),
    ],
)
def test_instrument_contract_decodes_mt5_filling_capability_bits(
    filling_mode: int, expected: tuple[str, ...]
) -> None:
    fake = FakeMT5()
    fake.info.filling_mode = filling_mode
    contract, codes = _adapter(fake).resolve_instrument("XAUUSD")
    assert codes == () and contract is not None
    assert contract.filling_modes == expected
    assert "RETURN" not in contract.filling_modes


def test_return_is_separate_from_mt5_filling_capability_bits() -> None:
    fake = FakeMT5()
    fake.info.filling_mode = 0
    fake.info.trade_exemode = fake.SYMBOL_TRADE_EXECUTION_INSTANT
    contract, codes = _adapter(fake).resolve_instrument("XAUUSD")
    assert codes == () and contract is not None
    assert contract.filling_modes == ("RETURN",)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"quantity": None}, "MT5_INVALID_VOLUME"), ({"quantity": 0.05}, "MT5_INVALID_VOLUME"),
        ({"quantity": 10.1}, "MT5_INVALID_VOLUME"), ({"quantity": 0.15}, "MT5_INVALID_VOLUME"),
        ({"order_type": "LIMIT"}, "MT5_UNSUPPORTED_ORDER_TYPE"),
        ({"filling_mode": "IOC"}, "MT5_UNSUPPORTED_FILLING_MODE"),
        ({"filling_mode": "BOOK_OR_CANCEL"}, "MT5_UNSUPPORTED_FILLING_MODE"),
        ({"requested_entry": 100.001}, "MT5_INVALID_PRICE"),
    ],
)
def test_invalid_mt5_constraints_fail_closed(change, expected) -> None:
    result = _adapter().prepare(_intent_for_mt5(**change))
    assert result.status is ExecutionStatus.NOT_EXECUTED
    assert expected in result.reason_codes


def test_market_closed_and_account_mismatch_are_rejected() -> None:
    closed = FakeMT5(); closed.tick = None
    assert "MT5_MARKET_CLOSED" in _adapter(closed).prepare(_intent_for_mt5()).reason_codes
    assert "MT5_ACCOUNT_UNAVAILABLE" in _adapter().prepare(_intent_for_mt5(account_id="other")).reason_codes


def test_read_only_evidence_never_initializes_or_submits_and_requires_connected_runtime() -> None:
    fake = FakeMT5()
    fake.initialize_calls = 0
    fake.login_calls = 0
    original_initialize, original_login = fake.initialize, fake.login
    fake.initialize = lambda **kwargs: (setattr(fake, "initialize_calls", fake.initialize_calls + 1), original_initialize(**kwargs))[1]
    fake.login = lambda *args, **kwargs: (setattr(fake, "login_calls", fake.login_calls + 1), original_login(*args, **kwargs))[1]
    fake.tick.time_msc = 1_790_000_000_000
    adapter = _adapter(fake)

    disconnected, codes = adapter.read_only_evidence_snapshot(
        canonical_symbol="XAUUSD", broker_symbol="XAUUSD.a"
    )
    assert disconnected is None and codes == ("MT5_READ_ONLY_RUNTIME_NOT_CONNECTED",)
    assert fake.initialize_calls == fake.login_calls == fake.order_send_calls == 0

    adapter._state = MT5SessionState.CONNECTED
    snapshot, codes = adapter.read_only_evidence_snapshot(
        canonical_symbol="XAUUSD", broker_symbol="XAUUSD.a"
    )
    assert codes == () and snapshot is not None
    assert snapshot.instrument.broker_symbol == "XAUUSD.a"
    assert fake.initialize_calls == fake.login_calls == fake.order_send_calls == 0


def test_read_only_evidence_rejects_malformed_quote_without_reconnecting() -> None:
    fake = FakeMT5()
    fake.tick.time_msc = 1_790_000_000_000
    fake.tick.bid, fake.tick.ask = 101.0, 100.0
    adapter = _adapter(fake)
    adapter._state = MT5SessionState.CONNECTED
    snapshot, codes = adapter.read_only_evidence_snapshot(
        canonical_symbol="XAUUSD", broker_symbol="XAUUSD.a"
    )
    assert snapshot is None and codes == ("MT5_READ_ONLY_EVIDENCE_MALFORMED",)
    assert fake.order_send_calls == 0
