"""Thread-safe bounded storage for provider-neutral economic events."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from hashlib import sha256
import json
from threading import RLock

from app.evidence.models import EconomicEventEvidence, EconomicEventSnapshot
from app.models.economic_events import EconomicReleaseStatus, NormalizedEconomicEvent


class EconomicEventStore:
    """Retain immutable lifecycle versions with deterministic conflict handling."""

    def __init__(
        self,
        *,
        max_events: int = 500,
        max_version_identities: int | None = None,
    ) -> None:
        _validate_limit(max_events, "max_events")
        version_identity_limit = (
            max_events * 4
            if max_version_identities is None
            else max_version_identities
        )
        _validate_limit(version_identity_limit, "max_version_identities")
        if version_identity_limit < max_events:
            raise ValueError("max_version_identities cannot be less than max_events")
        self._max_events = max_events
        self._max_version_identities = version_identity_limit
        self._events: dict[str, EconomicEventEvidence] = {}
        self._version_fingerprints: dict[
            tuple[str, str, int],
            frozenset[str],
        ] = {}
        self._lock = RLock()

    def ingest(self, event: NormalizedEconomicEvent) -> EconomicEventEvidence:
        """Insert one version, suppress exact duplicates, and reject conflicts."""
        evidence = EconomicEventEvidence.from_event(event)
        version_key = _version_key(evidence)
        with self._lock:
            known_fingerprints = self._version_fingerprints.get(version_key)
            if known_fingerprints is not None and (
                len(known_fingerprints) != 1
                or evidence.fingerprint not in known_fingerprints
            ):
                raise ValueError(
                    "conflicting economic-event content for the same source, "
                    "occurrence_id, and release_version"
                )
            if known_fingerprints is None:
                self._version_fingerprints[version_key] = frozenset(
                    {evidence.fingerprint}
                )

            existing = self._events.get(evidence.event_id)
            if existing is None or _duplicate_rank(evidence) < _duplicate_rank(existing):
                self._events[evidence.event_id] = evidence
            self._trim()
            self._trim_version_identities()
            return self._events.get(evidence.event_id, evidence)

    def snapshot(
        self,
        *,
        currency: str | None = None,
        country: str | None = None,
        statuses: Collection[EconomicReleaseStatus] | None = None,
        starts_at: datetime | None = None,
        ends_before: datetime | None = None,
    ) -> EconomicEventSnapshot:
        """Return a deterministically ordered immutable view of retained events."""
        start = _optional_utc(starts_at, "starts_at")
        end = _optional_utc(ends_before, "ends_before")
        if start is not None and end is not None and start > end:
            raise ValueError("starts_at cannot be later than ends_before")
        normalized_currency = currency.strip().upper() if currency else None
        normalized_country = country.strip().upper() if country else None
        accepted_statuses = frozenset(statuses) if statuses is not None else None

        with self._lock:
            events = tuple(
                sorted(
                    (
                        event
                        for event in self._events.values()
                        if normalized_currency is None
                        or event.currency == normalized_currency
                        if normalized_country is None
                        or event.country == normalized_country
                        if accepted_statuses is None
                        or event.release_status in accepted_statuses
                        if start is None or event.scheduled_at >= start
                        if end is None or event.scheduled_at < end
                    ),
                    key=economic_event_sort_key,
                )
            )
        return EconomicEventSnapshot(
            events=events,
            fingerprint=_snapshot_fingerprint(events),
        )

    def lifecycle_history(
        self,
        occurrence_id: str,
        *,
        source_id: str | None = None,
    ) -> tuple[EconomicEventEvidence, ...]:
        """Return all retained immutable versions for an occurrence."""
        normalized = occurrence_id.strip()
        if not normalized:
            raise ValueError("occurrence_id cannot be blank")
        with self._lock:
            return tuple(
                sorted(
                    (
                        event
                        for event in self._events.values()
                        if event.occurrence_id == normalized
                        and (
                            source_id is None
                            or event.provenance.source_id == source_id
                        )
                    ),
                    key=lambda item: (
                        item.provenance.source_id,
                        item.release_version,
                        item.event_id,
                    ),
                )
            )

    def latest_versions(self) -> tuple[EconomicEventEvidence, ...]:
        """Return the retained current version for each source occurrence."""
        return latest_economic_versions(self.snapshot().events)

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def version_identity_count(self) -> int:
        """Return the bounded count of retained conflict-detection identities."""
        with self._lock:
            return len(self._version_fingerprints)

    def _trim(self) -> None:
        retained = {
            event.event_id
            for event in sorted(
                self._events.values(),
                key=economic_event_retention_key,
                reverse=True,
            )[: self._max_events]
        }
        for event_id in tuple(self._events):
            if event_id not in retained:
                del self._events[event_id]

    def _trim_version_identities(self) -> None:
        if len(self._version_fingerprints) <= self._max_version_identities:
            return

        retained_event_keys = {_version_key(event) for event in self._events.values()}
        historical_capacity = self._max_version_identities - len(retained_event_keys)
        historical_keys = sorted(
            key
            for key in self._version_fingerprints
            if key not in retained_event_keys
        )
        retained_keys = retained_event_keys | set(
            historical_keys[:historical_capacity]
        )
        for key in tuple(self._version_fingerprints):
            if key not in retained_keys:
                del self._version_fingerprints[key]


def latest_economic_versions(
    events: tuple[EconomicEventEvidence, ...],
) -> tuple[EconomicEventEvidence, ...]:
    """Select one current version per provider occurrence, independent of order."""
    latest: dict[tuple[str, str], EconomicEventEvidence] = {}
    for event in events:
        key = (event.provenance.source_id, event.occurrence_id)
        existing = latest.get(key)
        if existing is None or _latest_rank(event) > _latest_rank(existing):
            latest[key] = event
    return tuple(sorted(latest.values(), key=economic_event_sort_key))


def economic_event_sort_key(
    event: EconomicEventEvidence,
) -> tuple[datetime, str, str, int, str]:
    return (
        event.scheduled_at,
        event.provenance.source_id,
        event.occurrence_id,
        event.release_version,
        event.event_id,
    )


def economic_event_retention_key(
    event: EconomicEventEvidence,
) -> tuple[datetime, int, str, str, str]:
    return (
        event.scheduled_at,
        event.release_version,
        event.provenance.source_id,
        event.occurrence_id,
        event.event_id,
    )


def _latest_rank(event: EconomicEventEvidence) -> tuple[int, datetime, str]:
    return (
        event.release_version,
        event.released_at or event.scheduled_at,
        event.event_id,
    )


def _version_key(event: EconomicEventEvidence) -> tuple[str, str, int]:
    return (
        event.provenance.source_id,
        event.occurrence_id,
        event.release_version,
    )


def _duplicate_rank(event: EconomicEventEvidence) -> tuple[datetime, str]:
    return (event.received_at, repr(event))


def _snapshot_fingerprint(events: tuple[EconomicEventEvidence, ...]) -> str:
    payload = [
        {"event_id": event.event_id, "fingerprint": event.fingerprint}
        for event in events
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _optional_utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


def _validate_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
