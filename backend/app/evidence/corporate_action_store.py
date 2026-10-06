"""Thread-safe bounded storage for provider-neutral corporate-action evidence."""

from __future__ import annotations

from hashlib import sha256
import json
from threading import RLock

from app.evidence.models import (
    CorporateActionEvidence,
    CorporateActionSnapshot,
    canonical_symbol,
    symbols_match,
)
from app.models.corporate_actions import CorporateActionInstrumentKey, NormalizedCorporateAction


class CorporateActionStore:
    """Retain bounded immutable action versions and conflict fingerprints only.

    The bounded fingerprint registry preserves same-version conflict detection
    after full records are evicted, without retaining evicted provider data.
    """

    def __init__(
        self,
        *,
        max_instruments: int = 20,
        max_occurrences_per_instrument: int = 500,
        max_versions_per_occurrence: int = 5,
        max_version_identities: int | None = None,
    ) -> None:
        _validate_limit(max_instruments, "max_instruments")
        _validate_limit(max_occurrences_per_instrument, "max_occurrences_per_instrument")
        _validate_limit(max_versions_per_occurrence, "max_versions_per_occurrence")
        full_capacity = max_instruments * max_occurrences_per_instrument * max_versions_per_occurrence
        identity_limit = full_capacity * 2 if max_version_identities is None else max_version_identities
        _validate_limit(identity_limit, "max_version_identities")
        if identity_limit < full_capacity:
            raise ValueError("max_version_identities cannot be less than retained full capacity")
        self._max_instruments = max_instruments
        self._max_occurrences_per_instrument = max_occurrences_per_instrument
        self._max_versions_per_occurrence = max_versions_per_occurrence
        self._max_version_identities = identity_limit
        self._actions: dict[
            CorporateActionInstrumentKey,
            dict[str, dict[int, CorporateActionEvidence]],
        ] = {}
        self._version_fingerprints: dict[tuple[str, str, str, int], frozenset[str]] = {}
        self._lock = RLock()

    def ingest(self, action: NormalizedCorporateAction) -> CorporateActionEvidence | None:
        if not isinstance(action, NormalizedCorporateAction):
            raise TypeError("action must be a NormalizedCorporateAction")
        evidence = CorporateActionEvidence.from_action(action)
        version_key = _version_key(evidence)
        with self._lock:
            known = self._version_fingerprints.get(version_key)
            if known is not None and (len(known) != 1 or evidence.fingerprint not in known):
                raise ValueError(
                    "conflicting corporate-action content for the same source, instrument, "
                    "occurrence_id, and action_version"
                )
            retained_keys = sorted(
                {*self._actions, action.instrument}, key=lambda item: item.instrument_id
            )[: self._max_instruments]
            if action.instrument not in retained_keys:
                return None
            for existing_key in tuple(self._actions):
                if existing_key not in retained_keys:
                    del self._actions[existing_key]
            if known is None:
                self._version_fingerprints[version_key] = frozenset({evidence.fingerprint})
            occurrences = self._actions.setdefault(action.instrument, {})
            versions = occurrences.setdefault(action.occurrence_id, {})
            existing = versions.get(action.action_version)
            if existing is None:
                versions[action.action_version] = evidence
            elif existing.fingerprint != evidence.fingerprint:  # pragma: no cover - registry guards
                raise ValueError("conflicting corporate action escaped the version registry")
            self._trim_versions(versions)
            self._trim_occurrences(occurrences)
            self._trim_empty_instruments()
            self._trim_version_identities()
            return self._actions.get(action.instrument, {}).get(action.occurrence_id, {}).get(
                action.action_version
            )

    def snapshot(self, *, symbol: str | None = None) -> CorporateActionSnapshot:
        requested_symbol = canonical_symbol(symbol)
        with self._lock:
            actions = tuple(
                sorted(
                    (
                        action
                        for key, occurrences in self._actions.items()
                        if requested_symbol is None or symbols_match(key.symbol, requested_symbol)
                        for versions in occurrences.values()
                        for action in versions.values()
                    ),
                    key=corporate_action_sort_key,
                )
            )
        return CorporateActionSnapshot(actions=actions, fingerprint=_snapshot_fingerprint(actions))

    def lifecycle_history(
        self,
        instrument: CorporateActionInstrumentKey,
        occurrence_id: str,
    ) -> tuple[CorporateActionEvidence, ...]:
        if not isinstance(instrument, CorporateActionInstrumentKey):
            raise TypeError("instrument must be a CorporateActionInstrumentKey")
        normalized = occurrence_id.strip()
        if not normalized:
            raise ValueError("occurrence_id cannot be blank")
        with self._lock:
            actions = tuple(self._actions.get(instrument, {}).get(normalized, {}).values())
        return tuple(sorted(actions, key=corporate_action_sort_key))

    @property
    def action_count(self) -> int:
        with self._lock:
            return sum(
                len(versions)
                for occurrences in self._actions.values()
                for versions in occurrences.values()
            )

    @property
    def instrument_count(self) -> int:
        with self._lock:
            return len(self._actions)

    @property
    def version_identity_count(self) -> int:
        with self._lock:
            return len(self._version_fingerprints)

    def _trim_versions(self, versions: dict[int, CorporateActionEvidence]) -> None:
        keep = set(sorted(versions, reverse=True)[: self._max_versions_per_occurrence])
        for version in tuple(versions):
            if version not in keep:
                del versions[version]

    def _trim_occurrences(self, occurrences: dict[str, dict[int, CorporateActionEvidence]]) -> None:
        keep = {
            occurrence_id
            for occurrence_id, versions in sorted(
                occurrences.items(), key=lambda item: _occurrence_retention_key(item[0], item[1]), reverse=True
            )[: self._max_occurrences_per_instrument]
        }
        for occurrence_id in tuple(occurrences):
            if occurrence_id not in keep:
                del occurrences[occurrence_id]

    def _trim_empty_instruments(self) -> None:
        for instrument in tuple(self._actions):
            if not self._actions[instrument]:
                del self._actions[instrument]

    def _trim_version_identities(self) -> None:
        if len(self._version_fingerprints) <= self._max_version_identities:
            return
        retained = {
            _version_key(action)
            for occurrences in self._actions.values()
            for versions in occurrences.values()
            for action in versions.values()
        }
        historical_capacity = self._max_version_identities - len(retained)
        historical = sorted(key for key in self._version_fingerprints if key not in retained)
        allowed = retained | set(historical[:historical_capacity])
        for key in tuple(self._version_fingerprints):
            if key not in allowed:
                del self._version_fingerprints[key]


def latest_corporate_action_versions(
    actions: tuple[CorporateActionEvidence, ...],
) -> tuple[CorporateActionEvidence, ...]:
    """Select current retained source versions per occurrence deterministically."""
    latest: dict[tuple[str, str, str], CorporateActionEvidence] = {}
    for action in actions:
        key = (action.provenance.source_id, action.instrument.instrument_id, action.occurrence_id)
        existing = latest.get(key)
        if existing is None or _latest_rank(action) > _latest_rank(existing):
            latest[key] = action
    return tuple(sorted(latest.values(), key=corporate_action_sort_key))


def corporate_action_sort_key(action: CorporateActionEvidence) -> tuple[float, str, str, int, str]:
    reference = action.effective_at or action.published_at
    return (
        reference.timestamp(),
        action.provenance.source_id,
        action.instrument.instrument_id,
        action.action_version,
        action.action_id,
    )


def _version_key(action: CorporateActionEvidence) -> tuple[str, str, str, int]:
    return (
        action.provenance.source_id,
        action.instrument.instrument_id,
        action.occurrence_id,
        action.action_version,
    )


def _latest_rank(action: CorporateActionEvidence) -> tuple[int, float, str]:
    return (action.action_version, (action.effective_at or action.published_at).timestamp(), action.action_id)


def _occurrence_retention_key(
    occurrence_id: str,
    versions: dict[int, CorporateActionEvidence],
) -> tuple[float, int, str]:
    latest = max(versions.values(), key=_latest_rank)
    return ((latest.effective_at or latest.published_at).timestamp(), latest.action_version, occurrence_id)


def _snapshot_fingerprint(actions: tuple[CorporateActionEvidence, ...]) -> str:
    payload = [{"action_id": item.action_id, "fingerprint": item.fingerprint} for item in actions]
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
