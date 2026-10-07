from app.execution.mt5_adapter import MT5ExecutionAdapter, MT5ExecutionMode
from app.models.execution import ExecutionStatus
from tests.test_mt5_adapter import FakeMT5, _intent_for_mt5, _settings


def _adapter(mode):
    fake = FakeMT5()
    return fake, MT5ExecutionAdapter(settings=_settings(), mt5_module=fake, symbol_mappings={"XAUUSD": "XAUUSD.a"}, execution_mode=mode)


def test_dry_run_builds_only_explicit_sanitized_request() -> None:
    fake, adapter = _adapter(MT5ExecutionMode.DRY_RUN)
    result = adapter.prepare(_intent_for_mt5(quantity=1.0, order_type="MARKET", filling_mode="FOK"))
    assert result.status is ExecutionStatus.DRY_RUN
    assert "MT5_DRY_RUN" in result.reason_codes
    assert dict(result.safe_request)["broker_symbol"] == "XAUUSD.a"
    assert fake.order_send_calls == 0


def test_disabled_read_only_and_live_modes_never_submit() -> None:
    for mode in (MT5ExecutionMode.DISABLED, MT5ExecutionMode.READ_ONLY, MT5ExecutionMode.LIVE):
        fake, adapter = _adapter(mode)
        result = adapter.prepare(_intent_for_mt5())
        assert result.status is ExecutionStatus.NOT_EXECUTED
        assert fake.order_send_calls == 0
        assert adapter.submit(_intent_for_mt5()).status is ExecutionStatus.NOT_EXECUTED
        assert fake.order_send_calls == 0


def test_missing_explicit_order_inputs_fail_before_request_build() -> None:
    _fake, adapter = _adapter(MT5ExecutionMode.DRY_RUN)
    result = adapter.prepare(_intent_for_mt5(quantity=None, order_type=None, filling_mode=None))
    assert set(result.reason_codes) >= {"MT5_INVALID_VOLUME", "MT5_UNSUPPORTED_ORDER_TYPE", "MT5_UNSUPPORTED_FILLING_MODE"}
