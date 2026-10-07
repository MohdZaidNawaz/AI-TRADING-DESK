"""Synthetic response-classification coverage; no MT5 terminal is touched."""
from types import SimpleNamespace

import pytest

from app.execution.mt5_response import MT5ResponseDisposition, classify_mt5_response


class FakeMT5Codes:
    TRADE_RETCODE_DONE = 1
    TRADE_RETCODE_PLACED = 2
    TRADE_RETCODE_REJECT = 3
    TRADE_RETCODE_INVALID = 4
    TRADE_RETCODE_NO_MONEY = 5
    TRADE_RETCODE_MARKET_CLOSED = 6
    TRADE_RETCODE_INVALID_VOLUME = 7
    TRADE_RETCODE_INVALID_FILL = 8


@pytest.mark.parametrize(
    ("retcode", "expected", "reason"),
    [
        (1, MT5ResponseDisposition.SUBMITTED, "MT5_SUBMITTED"),
        (3, MT5ResponseDisposition.REJECTED, "MT5_REJECTED"),
        (4, MT5ResponseDisposition.INVALID_REQUEST, "MT5_INVALID_REQUEST"),
        (5, MT5ResponseDisposition.INSUFFICIENT_MARGIN, "MT5_INSUFFICIENT_MARGIN"),
        (6, MT5ResponseDisposition.MARKET_CLOSED, "MT5_MARKET_CLOSED"),
        (7, MT5ResponseDisposition.INVALID_VOLUME, "MT5_INVALID_VOLUME"),
        (8, MT5ResponseDisposition.INVALID_FILLING_MODE, "MT5_INVALID_FILLING_MODE"),
    ],
)
def test_known_synthetic_mt5_response_codes_are_classified(retcode, expected, reason) -> None:
    result = classify_mt5_response(FakeMT5Codes(), SimpleNamespace(retcode=retcode, raw_payload="must-not-be-kept"))
    assert result.disposition is expected
    assert result.reason_code == reason
    assert result.requires_reconciliation is False
    assert "raw_payload" not in repr(result)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [({"timed_out": True}, "MT5_TIMEOUT"), ({"connection_failed": True}, "MT5_CONNECTION_FAILURE"), ({}, "MT5_UNKNOWN_OUTCOME")],
)
def test_uncertain_mt5_outcomes_require_reconciliation_without_retry(kwargs, reason) -> None:
    result = classify_mt5_response(FakeMT5Codes(), SimpleNamespace(retcode=999), **kwargs)
    assert result.disposition is MT5ResponseDisposition.UNKNOWN
    assert result.reason_code == reason
    assert result.requires_reconciliation is True
