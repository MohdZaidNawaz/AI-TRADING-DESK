"""Thread-safe bounded storage for immutable derived portfolio-risk metrics."""

from __future__ import annotations

from hashlib import sha256
import json
from threading import RLock

from app.evidence.models import (
    PortfolioRiskEvidence,
    PortfolioRiskMetricsEvidence,
    PortfolioRiskMetricsEvidenceSnapshot,
)
from app.models.portfolio_risk import SourceAccountKey
from app.models.portfolio_risk_metrics import PortfolioRiskMetricsSnapshot


class PortfolioRiskMetricsStore:
    """Retain bounded metric snapshots with bounded post-eviction conflict memory."""

    def __init__(
        self,
        *,
        max_accounts: int = 100,
        max_snapshots_per_account: int = 500,
        max_snapshot_identities: int | None = None,
    ) -> None:
        _validate_limit(max_accounts, "max_accounts")
        _validate_limit(max_snapshots_per_account, "max_snapshots_per_account")
        capacity = max_accounts * max_snapshots_per_account
        identity_limit = capacity * 2 if max_snapshot_identities is None else max_snapshot_identities
        _validate_limit(identity_limit, "max_snapshot_identities")
        if identity_limit < capacity:
            raise ValueError("max_snapshot_identities cannot be less than retained capacity")
        self._max_accounts = max_accounts
        self._max_snapshots_per_account = max_snapshots_per_account
        self._max_snapshot_identities = identity_limit
        self._snapshots: dict[SourceAccountKey, dict[str, PortfolioRiskMetricsEvidence]] = {}
        self._fingerprints: dict[tuple[str, str, str, int, str], frozenset[str]] = {}
        self._lock = RLock()

    def ingest(
        self,
        snapshot: PortfolioRiskMetricsSnapshot,
        source_evidence: PortfolioRiskEvidence,
    ) -> PortfolioRiskMetricsEvidence | None:
        if not isinstance(snapshot, PortfolioRiskMetricsSnapshot):
            raise TypeError("snapshot must be a PortfolioRiskMetricsSnapshot")
        evidence = PortfolioRiskMetricsEvidence.from_snapshot(snapshot, source_evidence)
        identity = _identity(evidence)
        with self._lock:
            known = self._fingerprints.get(identity)
            if known is not None and (len(known) != 1 or evidence.fingerprint not in known):
                raise ValueError(
                    "conflicting portfolio-risk metric content for the same source, account, "
                    "source snapshot occurrence, version, and calculation version"
                )
            retained_accounts = sorted(
                {*self._snapshots, evidence.account}, key=lambda item: item.account_id
            )[: self._max_accounts]
            if evidence.account not in retained_accounts:
                return None
            for account in tuple(self._snapshots):
                if account not in retained_accounts:
                    del self._snapshots[account]
            if known is None:
                self._fingerprints[identity] = frozenset({evidence.fingerprint})
            snapshots = self._snapshots.setdefault(evidence.account, {})
            existing = snapshots.get(evidence.metrics_snapshot_id)
            if existing is None:
                snapshots[evidence.metrics_snapshot_id] = evidence
            elif existing.fingerprint != evidence.fingerprint:  # pragma: no cover - registry guard
                raise ValueError("conflicting portfolio-risk metric evidence escaped the registry")
            self._trim_account(snapshots)
            self._trim_empty_accounts()
            self._trim_identities()
            return self._snapshots.get(evidence.account, {}).get(evidence.metrics_snapshot_id)

    def snapshot(self) -> PortfolioRiskMetricsEvidenceSnapshot:
        with self._lock:
            snapshots = tuple(
                sorted(
                    (item for per_account in self._snapshots.values() for item in per_account.values()),
                    key=portfolio_risk_metrics_sort_key,
                )
            )
        return PortfolioRiskMetricsEvidenceSnapshot(
            snapshots=snapshots, fingerprint=_snapshot_fingerprint(snapshots)
        )

    @property
    def snapshot_count(self) -> int:
        with self._lock:
            return sum(len(items) for items in self._snapshots.values())

    @property
    def account_count(self) -> int:
        with self._lock:
            return len(self._snapshots)

    @property
    def identity_count(self) -> int:
        with self._lock:
            return len(self._fingerprints)

    def _trim_account(self, snapshots: dict[str, PortfolioRiskMetricsEvidence]) -> None:
        keep = {
            item.metrics_snapshot_id
            for item in sorted(
                snapshots.values(), key=portfolio_risk_metrics_sort_key, reverse=True
            )[: self._max_snapshots_per_account]
        }
        for key in tuple(snapshots):
            if key not in keep:
                del snapshots[key]

    def _trim_empty_accounts(self) -> None:
        for account in tuple(self._snapshots):
            if not self._snapshots[account]:
                del self._snapshots[account]

    def _trim_identities(self) -> None:
        if len(self._fingerprints) <= self._max_snapshot_identities:
            return
        retained = {_identity(item) for items in self._snapshots.values() for item in items.values()}
        historical_capacity = self._max_snapshot_identities - len(retained)
        historical = sorted(key for key in self._fingerprints if key not in retained)
        allowed = retained | set(historical[:historical_capacity])
        for key in tuple(self._fingerprints):
            if key not in allowed:
                del self._fingerprints[key]


def portfolio_risk_metrics_sort_key(
    snapshot: PortfolioRiskMetricsEvidence,
) -> tuple[str, str, int, str, str]:
    return (
        snapshot.account.account_id,
        snapshot.source_snapshot_occurrence_id,
        snapshot.source_snapshot_version,
        snapshot.as_of.isoformat(),
        snapshot.metrics_snapshot_id,
    )


def _identity(snapshot: PortfolioRiskMetricsEvidence) -> tuple[str, str, str, int, str]:
    return (
        snapshot.provenance.source_id,
        snapshot.account.account_id,
        snapshot.source_snapshot_occurrence_id,
        snapshot.source_snapshot_version,
        snapshot.calculation_version,
    )


def _snapshot_fingerprint(snapshots: tuple[PortfolioRiskMetricsEvidence, ...]) -> str:
    payload = [
        {"metrics_snapshot_id": item.metrics_snapshot_id, "fingerprint": item.fingerprint}
        for item in snapshots
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _validate_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
