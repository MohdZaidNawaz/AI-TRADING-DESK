"""Fail-closed persistent accounting for optional paid LLM narration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Literal
from uuid import uuid4

from app.models.llm_synthesis import LLMBudgetStatus, LLMProviderName, LLMUsageRecord

_MICRO_INR = Decimal("1000000")
_MILLION = Decimal("1000000")
_ZERO = Decimal("0")


class LLMBudgetError(RuntimeError):
    """Budget persistence is unavailable; paid dispatch must fail closed."""


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    monthly_budget_inr: Decimal = Decimal("500")
    usable_budget_inr: Decimal = Decimal("450")
    protected_reserve_inr: Decimal = Decimal("50")
    daily_limit_inr: Decimal = Decimal("25")
    maximum_request_estimate_inr: Decimal = Decimal("5")
    paid_provider_authorized: bool = False

    def __post_init__(self) -> None:
        values = (
            self.monthly_budget_inr,
            self.usable_budget_inr,
            self.protected_reserve_inr,
            self.daily_limit_inr,
            self.maximum_request_estimate_inr,
        )
        if any(value < _ZERO for value in values):
            raise ValueError("LLM budget amounts must be non-negative")
        if self.usable_budget_inr + self.protected_reserve_inr > self.monthly_budget_inr:
            raise ValueError("usable budget and reserve exceed monthly budget")


@dataclass(frozen=True, slots=True)
class ProviderPricing:
    provider: LLMProviderName
    tier: Literal["free", "paid"]
    input_inr_per_million: Decimal
    output_inr_per_million: Decimal

    def __post_init__(self) -> None:
        if self.input_inr_per_million < _ZERO or self.output_inr_per_million < _ZERO:
            raise ValueError("provider pricing must be non-negative")
        if self.tier == "free" and (
            self.input_inr_per_million != _ZERO or self.output_inr_per_million != _ZERO
        ):
            raise ValueError("free-tier pricing must be zero")
        if self.tier == "paid" and (
            self.input_inr_per_million <= _ZERO or self.output_inr_per_million <= _ZERO
        ):
            raise ValueError("paid provider pricing must be explicit and positive")

    def estimate_micros(self, input_tokens: int, output_tokens: int) -> int:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        amount = (
            Decimal(input_tokens) * self.input_inr_per_million
            + Decimal(output_tokens) * self.output_inr_per_million
        ) / _MILLION
        return int((amount * _MICRO_INR).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True, slots=True)
class BudgetAuthorization:
    allowed: bool
    request_id: str
    estimated_micros: int
    reason: Literal[
        "authorized", "pricing_unavailable", "paid_provider_not_authorized", "budget_exhausted"
    ]


def parse_provider_pricing(
    value: str | None, *, inr_per_usd: Decimal | None
) -> dict[LLMProviderName, ProviderPricing]:
    """Parse explicit configuration only; malformed or unknown entries are ineligible."""
    if not value:
        return {}
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    parsed: dict[LLMProviderName, ProviderPricing] = {}
    for name in ("openai", "anthropic", "gemini", "groq"):
        item = raw.get(name)
        if not isinstance(item, dict):
            continue
        tier = item.get("tier")
        currency = item.get("currency")
        if tier not in ("free", "paid") or currency not in ("INR", "USD"):
            continue
        input_rate = _decimal(item.get("input_per_million"))
        output_rate = _decimal(item.get("output_per_million"))
        if input_rate is None or output_rate is None:
            continue
        if currency == "USD":
            if inr_per_usd is None or inr_per_usd <= _ZERO:
                continue
            input_rate *= inr_per_usd
            output_rate *= inr_per_usd
        try:
            parsed[name] = ProviderPricing(
                provider=name,  # type: ignore[arg-type]
                tier=tier,
                input_inr_per_million=input_rate,
                output_inr_per_million=output_rate,
            )
        except ValueError:
            continue
    return parsed


class LLMBudgetLedger:
    """SQLite reservations serialize concurrent requests before any paid dispatch."""

    def __init__(self, path: Path, policy: BudgetPolicy) -> None:
        self._path = path
        self._policy = policy
        self._lock = RLock()
        self._initialize()

    @property
    def policy(self) -> BudgetPolicy:
        return self._policy

    def authorize(
        self,
        *,
        provider: LLMProviderName,
        model: str,
        pricing: ProviderPricing | None,
        input_tokens: int,
        output_tokens: int,
        now: datetime | None = None,
    ) -> BudgetAuthorization:
        request_id = str(uuid4())
        observed = _utc(now or datetime.now(UTC))
        if pricing is None:
            return BudgetAuthorization(False, request_id, 0, "pricing_unavailable")
        if pricing.provider != provider:
            return BudgetAuthorization(False, request_id, 0, "pricing_unavailable")
        estimated = pricing.estimate_micros(input_tokens, output_tokens)
        if pricing.tier == "paid" and not self._policy.paid_provider_authorized:
            return BudgetAuthorization(False, request_id, estimated, "paid_provider_not_authorized")
        if pricing.tier == "paid" and not self._within_limits(estimated, observed):
            return BudgetAuthorization(False, request_id, estimated, "budget_exhausted")
        try:
            with self._lock, self._transaction() as connection:
                if pricing.tier == "paid" and not self._within_limits(estimated, observed, connection):
                    return BudgetAuthorization(False, request_id, estimated, "budget_exhausted")
                connection.execute(
                    """INSERT INTO llm_usage_ledger (
                    request_id, provider, model, estimated_input_tokens,
                    estimated_output_tokens, input_tokens, output_tokens, estimated_micros,
                    actual_micros, state, created_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL, 'reserved', ?, NULL)""",
                    (
                        request_id,
                        provider,
                        model,
                        input_tokens,
                        output_tokens,
                        estimated,
                        observed.isoformat(),
                    ),
                )
            return BudgetAuthorization(True, request_id, estimated, "authorized")
        except (sqlite3.DatabaseError, OSError) as error:
            raise LLMBudgetError("LLM budget ledger is unavailable") from error

    def complete(
        self,
        authorization: BudgetAuthorization,
        *,
        pricing: ProviderPricing,
        input_tokens: int | None,
        output_tokens: int | None,
        now: datetime | None = None,
    ) -> None:
        if not authorization.allowed:
            return
        actual = authorization.estimated_micros
        if input_tokens is not None and output_tokens is not None:
            actual = pricing.estimate_micros(input_tokens, output_tokens)
        observed = _utc(now or datetime.now(UTC))
        try:
            with self._lock, self._transaction() as connection:
                row = connection.execute(
                    "SELECT state FROM llm_usage_ledger WHERE request_id = ?", (authorization.request_id,)
                ).fetchone()
                if row is None or row[0] != "reserved":
                    raise LLMBudgetError("LLM budget reservation is invalid")
                connection.execute(
                    """UPDATE llm_usage_ledger SET input_tokens = COALESCE(?, input_tokens),
                    output_tokens = COALESCE(?, output_tokens),
                    actual_micros = ?, state = 'completed', completed_at = ? WHERE request_id = ?""",
                    (
                        input_tokens,
                        output_tokens,
                        actual,
                        observed.isoformat(),
                        authorization.request_id,
                    ),
                )
        except LLMBudgetError:
            raise
        except (sqlite3.DatabaseError, OSError) as error:
            raise LLMBudgetError("LLM budget ledger is unavailable") from error

    def release(self, authorization: BudgetAuthorization) -> None:
        if not authorization.allowed:
            return
        try:
            with self._lock, self._transaction() as connection:
                connection.execute(
                    "UPDATE llm_usage_ledger SET state = 'released' WHERE request_id = ? AND state = 'reserved'",
                    (authorization.request_id,),
                )
        except (sqlite3.DatabaseError, OSError) as error:
            raise LLMBudgetError("LLM budget ledger is unavailable") from error

    def status(self, now: datetime | None = None) -> LLMBudgetStatus:
        observed = _utc(now or datetime.now(UTC))
        try:
            with self._lock:
                connection = self._connect()
                try:
                    monthly = self._committed(connection, _month_start(observed), observed)
                    daily = self._committed(connection, _day_start(observed), observed)
                    monthly_spent = self._spent(
                        connection, _month_start(observed), observed
                    )
                    daily_spent = self._spent(
                        connection, _day_start(observed), observed
                    )
                    pending_reserved = connection.execute(
                        """SELECT COALESCE(SUM(estimated_micros), 0)
                        FROM llm_usage_ledger WHERE state = 'reserved'"""
                    ).fetchone()[0]
                    pending = connection.execute(
                        "SELECT COUNT(*) FROM llm_usage_ledger WHERE state = 'reserved'"
                    ).fetchone()[0]
                finally:
                    connection.close()
        except (sqlite3.DatabaseError, OSError) as error:
            raise LLMBudgetError("LLM budget ledger is unavailable") from error
        return LLMBudgetStatus(
            monthly_budget_inr=self._policy.monthly_budget_inr,
            usable_budget_inr=self._policy.usable_budget_inr,
            protected_reserve_inr=self._policy.protected_reserve_inr,
            daily_limit_inr=self._policy.daily_limit_inr,
            maximum_request_estimate_inr=self._policy.maximum_request_estimate_inr,
            paid_provider_authorized=self._policy.paid_provider_authorized,
            monthly_committed_inr=_inr(monthly),
            daily_committed_inr=_inr(daily),
            monthly_spent_inr=_inr(monthly_spent),
            daily_spent_inr=_inr(daily_spent),
            pending_reserved_inr=_inr(int(pending_reserved)),
            remaining_usable_inr=max(_ZERO, self._policy.usable_budget_inr - _inr(monthly)),
            pending_request_count=int(pending),
        )

    def usage(self, *, limit: int = 100) -> tuple[LLMUsageRecord, ...]:
        bounded_limit = min(max(limit, 1), 500)
        try:
            with self._lock:
                connection = self._connect()
                try:
                    rows = connection.execute(
                        """SELECT request_id, provider, model, estimated_input_tokens,
                        estimated_output_tokens, input_tokens, output_tokens, estimated_micros,
                        actual_micros, state, created_at
                        FROM llm_usage_ledger ORDER BY created_at DESC, request_id DESC LIMIT ?""",
                        (bounded_limit,),
                    ).fetchall()
                finally:
                    connection.close()
        except (sqlite3.DatabaseError, OSError) as error:
            raise LLMBudgetError("LLM budget ledger is unavailable") from error
        return tuple(
            LLMUsageRecord(
                request_id=row[0],
                provider=row[1],
                model=row[2],
                estimated_input_tokens=row[3],
                estimated_output_tokens=row[4],
                input_tokens=row[5],
                output_tokens=row[6],
                estimated_cost_inr=_inr(row[7]),
                actual_cost_inr=_inr(row[8]) if row[8] is not None else None,
                state=row[9],
                timestamp=row[10],
            )
            for row in rows
        )

    def _within_limits(
        self, estimate: int, now: datetime, connection: sqlite3.Connection | None = None
    ) -> bool:
        if _inr(estimate) > self._policy.maximum_request_estimate_inr:
            return False
        if connection is None:
            return True
        monthly = self._committed(connection, _month_start(now), now)
        daily = self._committed(connection, _day_start(now), now)
        return (
            _inr(monthly + estimate) <= self._policy.usable_budget_inr
            and _inr(daily + estimate) <= self._policy.daily_limit_inr
        )

    def _committed(self, connection: sqlite3.Connection, start: datetime, end: datetime) -> int:
        row = connection.execute(
            """SELECT COALESCE(SUM(CASE WHEN state = 'completed' THEN actual_micros
            WHEN state = 'reserved' THEN estimated_micros ELSE 0 END), 0)
            FROM llm_usage_ledger WHERE created_at >= ? AND created_at <= ?""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        return int(row[0])

    def _spent(self, connection: sqlite3.Connection, start: datetime, end: datetime) -> int:
        row = connection.execute(
            """SELECT COALESCE(SUM(actual_micros), 0) FROM llm_usage_ledger
            WHERE state = 'completed' AND created_at >= ? AND created_at <= ?""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        return int(row[0])

    def _initialize(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS llm_usage_ledger (
                    request_id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                    estimated_input_tokens INTEGER NOT NULL,
                    estimated_output_tokens INTEGER NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, estimated_micros INTEGER NOT NULL,
                    actual_micros INTEGER, state TEXT NOT NULL CHECK(state IN ('reserved','completed','released')),
                    created_at TEXT NOT NULL, completed_at TEXT)"""
                )
                connection.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_window ON llm_usage_ledger(created_at, state)")
                columns = tuple(
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(llm_usage_ledger)"
                    ).fetchall()
                )
                if columns != (
                    "request_id",
                    "provider",
                    "model",
                    "estimated_input_tokens",
                    "estimated_output_tokens",
                    "input_tokens",
                    "output_tokens",
                    "estimated_micros",
                    "actual_micros",
                    "state",
                    "created_at",
                    "completed_at",
                ):
                    raise sqlite3.DatabaseError("unexpected LLM usage ledger schema")
                connection.commit()
            finally:
                connection.close()
        except (sqlite3.DatabaseError, OSError) as error:
            raise LLMBudgetError("LLM budget ledger is unavailable") from error

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5.0, isolation_level=None)

    def _transaction(self):
        class _Transaction:
            def __init__(self, ledger: LLMBudgetLedger) -> None:
                self.ledger = ledger
                self.connection: sqlite3.Connection | None = None

            def __enter__(self) -> sqlite3.Connection:
                self.connection = self.ledger._connect()
                self.connection.execute("BEGIN IMMEDIATE")
                return self.connection

            def __exit__(self, kind, value, traceback) -> None:
                assert self.connection is not None
                try:
                    if kind is None:
                        self.connection.commit()
                    else:
                        self.connection.rollback()
                finally:
                    self.connection.close()

        return _Transaction(self)


def budget_policy_from_environment(env: dict[str, str | None]) -> BudgetPolicy:
    return BudgetPolicy(
        monthly_budget_inr=_decimal_or(env.get("FRIDAY_LLM_MONTHLY_BUDGET_INR"), Decimal("500")),
        usable_budget_inr=_decimal_or(env.get("FRIDAY_LLM_USABLE_BUDGET_INR"), Decimal("450")),
        protected_reserve_inr=_decimal_or(env.get("FRIDAY_LLM_PROTECTED_RESERVE_INR"), Decimal("50")),
        daily_limit_inr=_decimal_or(env.get("FRIDAY_LLM_DAILY_LIMIT_INR"), Decimal("25")),
        maximum_request_estimate_inr=_decimal_or(env.get("FRIDAY_LLM_MAX_REQUEST_ESTIMATE_INR"), Decimal("5")),
        paid_provider_authorized=(env.get("FRIDAY_LLM_PAID_PROVIDER_AUTHORIZED") or "").casefold() == "true",
    )


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= _ZERO else None


def _decimal_or(value: object, default: Decimal) -> Decimal:
    parsed = _decimal(value)
    return default if parsed is None else parsed


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _month_start(value: datetime) -> datetime:
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_start(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _inr(micros: int) -> Decimal:
    return Decimal(micros) / _MICRO_INR
