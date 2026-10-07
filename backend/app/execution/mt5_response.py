"""Pure, non-submitting classification of sanitized future MT5 responses."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MT5ResponseDisposition(str, Enum):
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    INVALID_REQUEST = "invalid_request"
    INSUFFICIENT_MARGIN = "insufficient_margin"
    MARKET_CLOSED = "market_closed"
    INVALID_VOLUME = "invalid_volume"
    INVALID_FILLING_MODE = "invalid_filling_mode"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MT5ResponseClassification:
    disposition: MT5ResponseDisposition
    reason_code: str
    requires_reconciliation: bool = False


def classify_mt5_response(mt5: object, response: object | None, *, timed_out: bool = False, connection_failed: bool = False) -> MT5ResponseClassification:
    """Classify known MT5 return codes without retaining a provider payload.

    The caller receives no retry instruction. Timeout, connection failure, and
    unrecognized outcomes are deliberately reconciliation-required.
    """
    if timed_out:
        return _unknown("MT5_TIMEOUT")
    if connection_failed:
        return _unknown("MT5_CONNECTION_FAILURE")
    code = getattr(response, "retcode", None)
    if code in _codes(mt5, "TRADE_RETCODE_DONE", "TRADE_RETCODE_PLACED"):
        return MT5ResponseClassification(MT5ResponseDisposition.SUBMITTED, "MT5_SUBMITTED")
    if code in _codes(mt5, "TRADE_RETCODE_REJECT"):
        return MT5ResponseClassification(MT5ResponseDisposition.REJECTED, "MT5_REJECTED")
    if code in _codes(mt5, "TRADE_RETCODE_INVALID"):
        return MT5ResponseClassification(MT5ResponseDisposition.INVALID_REQUEST, "MT5_INVALID_REQUEST")
    if code in _codes(mt5, "TRADE_RETCODE_NO_MONEY"):
        return MT5ResponseClassification(MT5ResponseDisposition.INSUFFICIENT_MARGIN, "MT5_INSUFFICIENT_MARGIN")
    if code in _codes(mt5, "TRADE_RETCODE_MARKET_CLOSED"):
        return MT5ResponseClassification(MT5ResponseDisposition.MARKET_CLOSED, "MT5_MARKET_CLOSED")
    if code in _codes(mt5, "TRADE_RETCODE_INVALID_VOLUME"):
        return MT5ResponseClassification(MT5ResponseDisposition.INVALID_VOLUME, "MT5_INVALID_VOLUME")
    if code in _codes(mt5, "TRADE_RETCODE_INVALID_FILL"):
        return MT5ResponseClassification(MT5ResponseDisposition.INVALID_FILLING_MODE, "MT5_INVALID_FILLING_MODE")
    return _unknown("MT5_UNKNOWN_OUTCOME")


def _codes(mt5: object, *names: str) -> frozenset[object]:
    return frozenset(getattr(mt5, name) for name in names if hasattr(mt5, name))


def _unknown(reason_code: str) -> MT5ResponseClassification:
    return MT5ResponseClassification(MT5ResponseDisposition.UNKNOWN, reason_code, requires_reconciliation=True)
