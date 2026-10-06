"""Thread-safe bounded storage for provider-neutral portfolio-risk evidence."""

from __future__ import annotations

from hashlib import sha256
import json
from threading import RLock

from app.evidence.models import PortfolioRiskEvidence, PortfolioRiskEvidenceSnapshot
from app.models.portfolio_risk import NormalizedPortfolioRiskSnapshot, SourceAccountKey


class PortfolioRiskStore:
    """Retain bounded immutable snapshot revisions and conflict fingerprints only."""

    def __init__(
        self,
        *,
        max_accounts: int = 100,
        max_occurrences_per_account: int = 500,
        max_versions_per_occurrence: int = 5,
        max_version_identities: int | None = None,
    ) -> None:
        _validate_limit(max_accounts, "max_accounts")
        _validate_limit(max_occurrences_per_account, "max_occurrences_per_account")
        _validate_limit(max_versions_per_occurrence, "max_versions_per_occurrence")
        full_capacity = max_accounts * max_occurrences_per_account * max_versions_per_occurrence
        identity_limit = full_capacity * 2 if max_version_identities is None else max_version_identities
        _validate_limit(identity_limit, "max_version_identities")
        if identity_limit < full_capacity:
            raise ValueError("max_version_identities cannot be less than retained full capacity")
        self._max_accounts = max_accounts
        self._max_occurrences_per_account = max_occurrences_per_account
        self._max_versions_per_occurrence = max_versions_per_occurrence
        self._max_version_identities = identity_limit
        self._snapshots: dict[SourceAccountKey, dict[str, dict[int, PortfolioRiskEvidence]]] = {}
        self._version_fingerprints: dict[tuple[str, str, str, int], frozenset[str]] = {}
        self._lock = RLock()

    def ingest(
        self,
        snapshot: NormalizedPortfolioRiskSnapshot,
    ) -> PortfolioRiskEvidence | None:
        if not isinstance(snapshot, NormalizedPortfolioRiskSnapshot):
            raise TypeError("snapshot must be a NormalizedPortfolioRiskSnapshot")
        evidence = PortfolioRiskEvidence.from_snapshot(snapshot)
        version_key = _version_key(evidence)
        with self._lock:
            known = self._version_fingerprints.get(version_key)
            if known is not None and (len(known) != 1 or evidence.fingerprint not in known):
                raise ValueError(
                    "conflicting portfolio-risk content for the same source, account, "
                    "snapshot occurrence, and snapshot version"
                )
            retained_accounts = sorted(
                {*self._snapshots, snapshot.account}, key=lambda item: item.account_id
            )[: self._max_accounts]
            if snapshot.account not in retained_accounts:
                return None
            for existing_account in tuple(self._snapshots):
                if existing_account not in retained_accounts:
                    del self._snapshots[existing_account]
            if known is None:
                self._version_fingerprints[version_key] = frozenset({evidence.fingerprint})
            occurrences = self._snapshots.setdefault(snapshot.account, {})
            versions = occurrences.setdefault(snapshot.snapshot_occurrence_id, {})
            existing = versions.get(snapshot.snapshot_version)
            if existing is None:
                versions[snapshot.snapshot_version] = evidence
            elif existing.fingerprint != evidence.fingerprint:  # pragma: no cover - registry guards
                raise ValueError("conflicting portfolio-risk evidence escaped the version registry")
            self._trim_versions(versions)
            self._trim_occurrences(occurrences)
            self._trim_empty_accounts()
            self._trim_version_identities()
            return self._snapshots.get(snapshot.account, {}).get(snapshot.snapshot_occurrence_id, {}).get(
                snapshot.snapshot_version
            )

    def snapshot(self) -> PortfolioRiskEvidenceSnapshot:
        with self._lock:
            snapshots = tuple(
                sorted(
                    (
                        item
                        for occurrences in self._snapshots.values()
                        for versions in occurrences.values()
                        for item in versions.values()
                    ),
                    key=portfolio_risk_sort_key,
                )
            )
        return PortfolioRiskEvidenceSnapshot(
            snapshots=snapshots,
            fingerprint=_snapshot_fingerprint(snapshots),
        )

    def revision_history(
        self,
        account: SourceAccountKey,
        snapshot_occurrence_id: str,
    ) -> tuple[PortfolioRiskEvidence, ...]:
        if not isinstance(account, SourceAccountKey):
            raise TypeError("account must be a SourceAccountKey")
        occurrence = snapshot_occurrence_id.strip()
        if not occurrence:
            raise ValueError("snapshot_occurrence_id cannot be blank")
        with self._lock:
            values = tuple(self._snapshots.get(account, {}).get(occurrence, {}).values())
        return tuple(sorted(values, key=portfolio_risk_sort_key))

    @property
    def snapshot_count(self) -> int:
        with self._lock:
            return sum(
                len(versions)
                for occurrences in self._snapshots.values()
                for versions in occurrences.values()
            )

    @property
    def account_count(self) -> int:
        with self._lock:
            return len(self._snapshots)

    @property
    def version_identity_count(self) -> int:
        with self._lock:
            return len(self._version_fingerprints)

    def _trim_versions(self, versions: dict[int, PortfolioRiskEvidence]) -> None:
        keep = set(sorted(versions, reverse=True)[: self._max_versions_per_occurrence])
        for version in tuple(versions):
            if version not in keep:
                del versions[version]

    def _trim_occurrences(self, occurrences: dict[str, dict[int, PortfolioRiskEvidence]]) -> None:
        keep = {
            occurrence
            for occurrence, versions in sorted(
                occurrences.items(), key=lambda item: _occurrence_retention_key(item[0], item[1]), reverse=True
            )[: self._max_occurrences_per_account]
        }
        for occurrence in tuple(occurrences):
            if occurrence not in keep:
                del occurrences[occurrence]

    def _trim_empty_accounts(self) -> None:
        for account in tuple(self._snapshots):
            if not self._snapshots[account]:
                del self._snapshots[account]

    def _trim_version_identities(self) -> None:
        if len(self._version_fingerprints) <= self._max_version_identities:
            return
        retained = {
            _version_key(item)
            for occurrences in self._snapshots.values()
            for versions in occurrences.values()
            for item in versions.values()
        }
        historical_capacity = self._max_version_identities - len(retained)
        historical = sorted(key for key in self._version_fingerprints if key not in retained)
        allowed = retained | set(historical[:historical_capacity])
        for key in tuple(self._version_fingerprints):
            if key not in allowed:
                del self._version_fingerprints[key]


def portfolio_risk_sort_key(
    snapshot: PortfolioRiskEvidence,
) -> tuple[str, str, str, int, str]:
    return (
        snapshot.account.account_id,
        snapshot.snapshot_occurrence_id,
        snapshot.as_of.isoformat(),
        snapshot.snapshot_version,
        snapshot.snapshot_id,
    )


def _version_key(snapshot: PortfolioRiskEvidence) -> tuple[str, str, str, int]:
    return (
        snapshot.provenance.source_id,
        snapshot.account.account_id,
        snapshot.snapshot_occurrence_id,
        snapshot.snapshot_version,
    )


def _occurrence_retention_key(
    occurrence: str,
    versions: dict[int, PortfolioRiskEvidence],
) -> tuple[str, int, str]:
    latest = max(versions.values(), key=lambda item: (item.snapshot_version, item.as_of, item.snapshot_id))
    return (latest.as_of.isoformat(), latest.snapshot_version, occurrence)


def _snapshot_fingerprint(snapshots: tuple[PortfolioRiskEvidence, ...]) -> str:
    payload = [
        {"snapshot_id": item.snapshot_id, "fingerprint": item.fingerprint}
        for item in snapshots
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _validate_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
