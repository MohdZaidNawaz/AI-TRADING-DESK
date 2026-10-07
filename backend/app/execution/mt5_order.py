"""MT5-specific order-request construction for dry-run validation only."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.execution import ExecutionIntent
from app.models.mt5 import BrokerInstrumentContract
from app.models.proposals import TradeDirection


@dataclass(frozen=True, slots=True)
class MT5OrderRequest:
    """An internal request.  It is built but never submitted in Phase 9."""

    symbol: str
    action: int
    order_type: int
    volume: float
    price: float
    stop_loss: float
    take_profit: float
    filling_mode: int
    time_in_force: str | None
    magic: int
    comment: str

    def safe_items(self) -> tuple[tuple[str, str], ...]:
        """Safe, credential-free representation for a dry-run result."""
        return (
            ("action", "deal"),
            ("broker_symbol", self.symbol),
            ("comment", self.comment),
            ("filling_mode", str(self.filling_mode)),
            ("order_type", str(self.order_type)),
            ("price", str(self.price)),
            ("stop_loss", str(self.stop_loss)),
            ("take_profit", str(self.take_profit)),
            ("time_in_force", self.time_in_force or ""),
            ("volume", str(self.volume)),
        )


def build_mt5_order_request(
    *,
    intent: ExecutionIntent,
    instrument: BrokerInstrumentContract,
    mt5: object,
) -> MT5OrderRequest:
    """Translate only explicit, already-validated values to MT5 constants."""
    if intent.quantity is None or intent.order_type is None or intent.filling_mode is None:
        raise ValueError("MT5 explicit quantity, order type, and filling mode are required")
    direction_type = (
        getattr(mt5, "ORDER_TYPE_BUY")
        if intent.direction is TradeDirection.LONG
        else getattr(mt5, "ORDER_TYPE_SELL")
    )
    filling = _filling_constant(mt5, intent.filling_mode)
    return MT5OrderRequest(
        symbol=instrument.broker_symbol,
        action=getattr(mt5, "TRADE_ACTION_DEAL"),
        order_type=direction_type,
        volume=float(intent.quantity),
        price=float(intent.requested_entry),
        stop_loss=float(intent.stop_loss),
        take_profit=float(intent.take_profit),
        filling_mode=filling,
        time_in_force=intent.time_in_force,
        magic=0,
        comment=f"FRIDAY:{intent.execution_intent_id[:24]}",
    )


def _filling_constant(mt5: object, mode: str) -> int:
    normalized = mode.strip().upper()
    values = {
        "FOK": "ORDER_FILLING_FOK",
        "IOC": "ORDER_FILLING_IOC",
        "RETURN": "ORDER_FILLING_RETURN",
    }
    constant = values.get(normalized)
    if constant is None:
        raise ValueError("unsupported MT5 filling mode")
    return getattr(mt5, constant)
