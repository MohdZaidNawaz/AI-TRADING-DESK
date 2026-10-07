"""MT5-specific adapter with a read-only and dry-run-only Phase 9 boundary.

The module imports MetaTrader5 lazily, accepts an injected module for tests, and
never calls ``order_send``.  Live submission is intentionally deferred to the
separate Phase 10 safety boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import importlib
from typing import Mapping, Sequence

from app.config import get_mt5_settings
from app.execution.mt5_order import build_mt5_order_request
from app.models.execution import ExecutionIntent, ExecutionResult, ExecutionStatus
from app.models.mt5 import (
    BrokerInstrumentContract,
    MT5AccountSnapshot,
    MT5ExecutionMode,
    MT5OrderStatusSnapshot,
    MT5PositionSnapshot,
    MT5SessionState,
)
from app.models.portfolio_risk import SourceAccountKey


MT5_SOURCE_ID = "mt5"


@dataclass(frozen=True, slots=True)
class MT5Settings:
    """Local terminal settings; the password is intentionally hidden in repr."""

    login: int | None
    password: str | None = field(default=None, repr=False)
    server: str | None = None
    path: str | None = None
    configured_mode: str | None = None

    @classmethod
    def from_environment(cls) -> "MT5Settings":
        values = get_mt5_settings()
        login_value = values["MT5_LOGIN"]
        try:
            login = int(login_value) if login_value is not None else None
        except ValueError:
            login = None
        return cls(
            login=login,
            password=values["MT5_PASSWORD"],
            server=values["MT5_SERVER"],
            path=values["MT5_PATH"],
            configured_mode=values["MT5_EXECUTION_MODE"],
        )

    @property
    def is_configured(self) -> bool:
        return self.login is not None and self.password is not None and self.server is not None


class MT5ExecutionAdapter:
    """Injected execution adapter; all order submission remains hard-blocked."""

    def __init__(
        self,
        *,
        settings: MT5Settings | None = None,
        mt5_module: object | None = None,
        symbol_mappings: Mapping[str, str | Sequence[str]] | None = None,
        execution_mode: MT5ExecutionMode | None = None,
    ) -> None:
        self._settings = settings or MT5Settings.from_environment()
        self._mt5 = mt5_module
        self._symbol_mappings = _freeze_mappings(symbol_mappings or {})
        self._execution_mode = execution_mode or _safe_mode(self._settings.configured_mode)
        self._state = MT5SessionState.DISCONNECTED
        self._last_code: str | None = None

    @property
    def session_state(self) -> MT5SessionState:
        return self._state

    @property
    def execution_mode(self) -> MT5ExecutionMode:
        return self._execution_mode

    def availability(self) -> str:
        """Initialize a terminal session only when a caller explicitly uses it."""
        codes = self._ensure_session()
        return "MT5_CONNECTED" if not codes else codes[0]

    def account_information(self) -> tuple[MT5AccountSnapshot | None, tuple[str, ...]]:
        codes = self._ensure_session()
        if codes:
            return None, codes
        account = self._account_snapshot()
        return (account, ()) if account is not None else (None, ("MT5_ACCOUNT_UNAVAILABLE",))

    def positions(self) -> tuple[MT5PositionSnapshot, ...]:
        if self._ensure_session():
            return ()
        values = getattr(self._mt5, "positions_get")() or ()
        return tuple(
            MT5PositionSnapshot(
                ticket=int(value.ticket),
                symbol=str(value.symbol),
                volume=float(value.volume),
                price_open=float(value.price_open),
                stop_loss=_optional_number(value, "sl"),
                take_profit=_optional_number(value, "tp"),
                profit=_optional_number(value, "profit"),
                magic=_optional_int(value, "magic"),
                comment=_optional_text(value, "comment"),
            )
            for value in sorted(values, key=lambda item: int(item.ticket))
        )

    def order_status(self, ticket: int) -> MT5OrderStatusSnapshot | None:
        """Read a broker order status only; no state transition or reconciliation."""
        if self._ensure_session():
            return None
        values = getattr(self._mt5, "orders_get")(ticket=ticket) or ()
        if not values:
            return None
        value = sorted(values, key=lambda item: int(item.ticket))[0]
        return MT5OrderStatusSnapshot(
            ticket=int(value.ticket), symbol=str(value.symbol),
            volume_initial=float(value.volume_initial), volume_current=float(value.volume_current),
            price_open=float(value.price_open), stop_loss=_optional_number(value, "sl"),
            take_profit=_optional_number(value, "tp"), state=str(getattr(value, "state", "unknown")),
            magic=_optional_int(value, "magic"), comment=_optional_text(value, "comment"),
        )

    def resolve_instrument(
        self, canonical_symbol: str
    ) -> tuple[BrokerInstrumentContract | None, tuple[str, ...]]:
        codes = self._ensure_session()
        if codes:
            return None, codes
        candidates = self._symbol_mappings.get(_canonical_symbol(canonical_symbol))
        if candidates is None:
            return None, ("MT5_SYMBOL_MAPPING_MISSING",)
        if len(candidates) != 1:
            return None, ("MT5_SYMBOL_MAPPING_AMBIGUOUS",)
        broker_symbol = candidates[0]
        info = getattr(self._mt5, "symbol_info")(broker_symbol)
        if info is None:
            return None, ("MT5_SYMBOL_NOT_FOUND",)
        if not bool(getattr(info, "visible", False)):
            if not bool(getattr(self._mt5, "symbol_select")(broker_symbol, True)):
                return None, ("MT5_SYMBOL_NOT_VISIBLE",)
            info = getattr(self._mt5, "symbol_info")(broker_symbol)
            if info is None or not bool(getattr(info, "visible", False)):
                return None, ("MT5_SYMBOL_NOT_VISIBLE",)
        disabled_mode = getattr(self._mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)
        tradable = getattr(info, "trade_mode", disabled_mode) != disabled_mode
        tick = getattr(self._mt5, "symbol_info_tick")(broker_symbol)
        contract = BrokerInstrumentContract(
            canonical_symbol=_canonical_symbol(canonical_symbol), broker_symbol=broker_symbol,
            digits=int(getattr(info, "digits")), point=float(getattr(info, "point")),
            tick_size=float(getattr(info, "trade_tick_size", getattr(info, "point"))),
            tick_value=_optional_number(info, "trade_tick_value"),
            contract_size=_optional_number(info, "trade_contract_size"),
            minimum_volume=float(getattr(info, "volume_min")), maximum_volume=float(getattr(info, "volume_max")),
            volume_step=float(getattr(info, "volume_step")), trade_mode=getattr(info, "trade_mode", None),
            filling_modes=_filling_modes(self._mt5, info), order_modes=_order_modes(self._mt5, info),
            visible=True, tradable=tradable, quote_available=tick is not None,
        )
        if not contract.tradable:
            return contract, ("MT5_SYMBOL_NOT_TRADABLE",)
        if not contract.quote_available:
            return contract, ("MT5_MARKET_CLOSED",)
        return contract, ()

    def prepare(self, intent: ExecutionIntent) -> ExecutionResult:
        """Validate and optionally build a dry-run request; never submit an order."""
        now = datetime.now(UTC)
        account, account_codes = self.account_information()
        if account_codes:
            return _result(intent, now, ExecutionStatus.NOT_EXECUTED, account_codes, self._state.value)
        if account is None or account.account.account_id != intent.account_id:
            return _result(intent, now, ExecutionStatus.NOT_EXECUTED, ("MT5_ACCOUNT_UNAVAILABLE",), self._state.value)
        if not account.trading_allowed or not account.terminal_trade_allowed:
            return _result(intent, now, ExecutionStatus.NOT_EXECUTED, ("MT5_TRADING_DISABLED",), self._state.value)
        instrument, codes = self.resolve_instrument(intent.symbol)
        if codes or instrument is None:
            return _result(intent, now, ExecutionStatus.NOT_EXECUTED, codes or ("MT5_SYMBOL_NOT_FOUND",), self._state.value)
        validation = _validate_intent(intent, instrument)
        if validation:
            return _result(intent, now, ExecutionStatus.NOT_EXECUTED, validation, self._state.value)
        if self._execution_mode is MT5ExecutionMode.DISABLED:
            return _result(intent, now, ExecutionStatus.NOT_EXECUTED, ("MT5_EXECUTION_DISABLED",), self._state.value)
        if self._execution_mode is MT5ExecutionMode.READ_ONLY:
            return _result(intent, now, ExecutionStatus.NOT_EXECUTED, ("MT5_READ_ONLY",), self._state.value)
        try:
            request = build_mt5_order_request(intent=intent, instrument=instrument, mt5=self._mt5)
        except (AttributeError, ValueError):
            return _result(intent, now, ExecutionStatus.NOT_EXECUTED, ("MT5_REQUEST_INVALID",), self._state.value)
        if self._execution_mode is MT5ExecutionMode.DRY_RUN:
            return _result(intent, now, ExecutionStatus.DRY_RUN, ("MT5_DRY_RUN",), "MT5_DRY_RUN", request.safe_items())
        # Even an explicitly supplied LIVE setting cannot cross the Phase 10 gate.
        return _result(intent, now, ExecutionStatus.NOT_EXECUTED, ("MT5_EXECUTION_DISABLED",), self._state.value)

    def submit(self, intent: ExecutionIntent) -> ExecutionResult:
        """Hard stop: Phase 9 has no reachable live submission path."""
        return _result(intent, datetime.now(UTC), ExecutionStatus.NOT_EXECUTED, ("MT5_EXECUTION_DISABLED",), self._state.value)

    def query(self, intent_id: str) -> ExecutionResult:
        return ExecutionResult(intent_id=intent_id, proposal_id="", status=ExecutionStatus.NOT_EXECUTED, reason_codes=("MT5_ORDER_LOOKUP_REQUIRES_BROKER_TICKET",), evaluated_at=datetime.now(UTC), adapter_status=self._state.value)

    def cancel(self, intent_id: str) -> ExecutionResult:
        return ExecutionResult(intent_id=intent_id, proposal_id="", status=ExecutionStatus.NOT_EXECUTED, reason_codes=("MT5_EXECUTION_DISABLED",), evaluated_at=datetime.now(UTC), adapter_status=self._state.value)

    def _ensure_session(self) -> tuple[str, ...]:
        if self._state is MT5SessionState.CONNECTED:
            return ()
        if not self._settings.is_configured:
            self._state = MT5SessionState.UNAVAILABLE
            return ("MT5_NOT_CONFIGURED",)
        self._state = MT5SessionState.INITIALIZING
        if self._mt5 is None:
            try:
                self._mt5 = importlib.import_module("MetaTrader5")
            except ModuleNotFoundError:
                self._state = MT5SessionState.UNAVAILABLE
                return ("MT5_NOT_INSTALLED",)
        initialize = getattr(self._mt5, "initialize", None)
        login = getattr(self._mt5, "login", None)
        if initialize is None or login is None:
            self._state = MT5SessionState.UNAVAILABLE
            return ("MT5_TERMINAL_UNAVAILABLE",)
        kwargs = {} if self._settings.path is None else {"path": self._settings.path}
        try:
            initialized = bool(initialize(**kwargs))
            authorized = initialized and bool(login(self._settings.login, password=self._settings.password, server=self._settings.server))
        except Exception:
            self._state = MT5SessionState.FAILED
            return ("MT5_CONNECTION_ERROR",)
        if not initialized:
            self._state = MT5SessionState.UNAVAILABLE
            return ("MT5_NOT_INITIALIZED",)
        if not authorized:
            self._state = MT5SessionState.FAILED
            return ("MT5_AUTHORIZATION_FAILED",)
        if self._account_snapshot() is None:
            self._state = MT5SessionState.DEGRADED
            return ("MT5_ACCOUNT_UNAVAILABLE",)
        self._state = MT5SessionState.CONNECTED
        return ()

    def _account_snapshot(self) -> MT5AccountSnapshot | None:
        if self._mt5 is None:
            return None
        info = getattr(self._mt5, "account_info", lambda: None)()
        if info is None:
            return None
        login = getattr(info, "login", None)
        server = _optional_text(info, "server") or self._settings.server
        if login is None or server is None:
            return None
        terminal = getattr(self._mt5, "terminal_info", lambda: None)()
        version = getattr(self._mt5, "version", lambda: None)()
        return MT5AccountSnapshot(
            account=SourceAccountKey(source_id=MT5_SOURCE_ID, source_account_id=str(login), account_reference=server),
            balance=float(getattr(info, "balance")), equity=float(getattr(info, "equity")),
            margin=float(getattr(info, "margin")), free_margin=float(getattr(info, "margin_free")),
            currency=str(getattr(info, "currency")), trading_allowed=bool(getattr(info, "trade_allowed", False)),
            terminal_trade_allowed=bool(getattr(terminal, "trade_allowed", False)),
            terminal_version=None if version is None else ".".join(str(item) for item in version),
        )


def _safe_mode(value: str | None) -> MT5ExecutionMode:
    try:
        return MT5ExecutionMode((value or MT5ExecutionMode.DISABLED.value).strip().lower())
    except ValueError:
        return MT5ExecutionMode.DISABLED


def _freeze_mappings(values: Mapping[str, str | Sequence[str]]) -> dict[str, tuple[str, ...]]:
    frozen: dict[str, tuple[str, ...]] = {}
    for canonical, candidates in values.items():
        source = (candidates,) if isinstance(candidates, str) else tuple(candidates)
        frozen[_canonical_symbol(canonical)] = tuple(sorted({str(value).strip() for value in source if str(value).strip()}))
    return frozen


def _canonical_symbol(value: str) -> str:
    return value.strip().upper()


def _filling_modes(mt5: object, info: object) -> tuple[str, ...]:
    actual = getattr(info, "filling_mode", None)
    modes = {"FOK": "ORDER_FILLING_FOK", "IOC": "ORDER_FILLING_IOC", "RETURN": "ORDER_FILLING_RETURN"}
    return tuple(name for name, constant in modes.items() if actual == getattr(mt5, constant, object()))


def _order_modes(mt5: object, info: object) -> tuple[str, ...]:
    market_bit = getattr(mt5, "SYMBOL_ORDER_MARKET", 1)
    return ("MARKET",) if int(getattr(info, "order_mode", 0)) & int(market_bit) else ()


def _validate_intent(intent: ExecutionIntent, instrument: BrokerInstrumentContract) -> tuple[str, ...]:
    codes: list[str] = []
    if intent.quantity is None:
        codes.append("MT5_INVALID_VOLUME")
    elif not _valid_volume(intent.quantity, instrument):
        codes.append("MT5_INVALID_VOLUME")
    if intent.order_type is None or intent.order_type.strip().upper() not in instrument.order_modes:
        codes.append("MT5_UNSUPPORTED_ORDER_TYPE")
    if intent.filling_mode is None or intent.filling_mode.strip().upper() not in instrument.filling_modes:
        codes.append("MT5_UNSUPPORTED_FILLING_MODE")
    if not all(_valid_price(value, instrument) for value in (intent.requested_entry, intent.stop_loss, intent.take_profit)):
        codes.append("MT5_INVALID_PRICE")
    return tuple(sorted(set(codes)))


def _valid_volume(value: float, instrument: BrokerInstrumentContract) -> bool:
    if value < instrument.minimum_volume or value > instrument.maximum_volume or instrument.volume_step <= 0:
        return False
    try:
        remainder = (Decimal(str(value)) - Decimal(str(instrument.minimum_volume))) % Decimal(str(instrument.volume_step))
        return remainder == 0
    except (InvalidOperation, ValueError):
        return False


def _valid_price(value: float, instrument: BrokerInstrumentContract) -> bool:
    if instrument.tick_size <= 0:
        return False
    try:
        decimal = Decimal(str(value))
        precision = Decimal(1).scaleb(-instrument.digits)
        if decimal.quantize(precision) != decimal:
            return False
        return decimal % Decimal(str(instrument.tick_size)) == 0
    except (InvalidOperation, ValueError):
        return False


def _result(intent: ExecutionIntent, now: datetime, status: ExecutionStatus, codes: tuple[str, ...], adapter_status: str, safe_request: tuple[tuple[str, str], ...] = ()) -> ExecutionResult:
    return ExecutionResult(intent_id=intent.execution_intent_id, proposal_id=intent.proposal_id, status=status, reason_codes=codes, evaluated_at=now, adapter_status=adapter_status, safe_request=safe_request)


def _optional_number(value: object, name: str) -> float | None:
    raw = getattr(value, name, None)
    return None if raw is None else float(raw)


def _optional_int(value: object, name: str) -> int | None:
    raw = getattr(value, name, None)
    return None if raw is None else int(raw)


def _optional_text(value: object, name: str) -> str | None:
    raw = getattr(value, name, None)
    return None if raw is None else str(raw).strip() or None
