"""Thread-safe bounded storage for provider-neutral issuer-reference evidence."""

from __future__ import annotations

from hashlib import sha256
import json
from threading import RLock

from app.evidence.models import (
    IssuerReferenceEvidence,
    IssuerReferenceSnapshot,
    canonical_symbol,
    symbols_match,
)
from app.models.issuer_reference import (
    IssuerInstrumentReferenceKey,
    NormalizedIssuerInstrumentReference,
)


class IssuerReferenceStore:
    """Retain bounded profile revisions and conflict fingerprints only.

    The registry preserves same-version conflict detection after full profile
    records are evicted, without retaining source payloads or unbounded history.
    """

    def __init__(
        self,
        *,
        max_instruments: int = 100,
        max_profiles_per_instrument: int = 5,
        max_versions_per_profile: int = 5,
        max_version_identities: int | None = None,
    ) -> None:
        _validate_limit(max_instruments, "max_instruments")
        _validate_limit(max_profiles_per_instrument, "max_profiles_per_instrument")
        _validate_limit(max_versions_per_profile, "max_versions_per_profile")
        full_capacity = max_instruments * max_profiles_per_instrument * max_versions_per_profile
        identity_limit = full_capacity * 2 if max_version_identities is None else max_version_identities
        _validate_limit(identity_limit, "max_version_identities")
        if identity_limit < full_capacity:
            raise ValueError("max_version_identities cannot be less than retained full capacity")
        self._max_instruments = max_instruments
        self._max_profiles_per_instrument = max_profiles_per_instrument
        self._max_versions_per_profile = max_versions_per_profile
        self._max_version_identities = identity_limit
        self._profiles: dict[
            IssuerInstrumentReferenceKey,
            dict[str, dict[int, IssuerReferenceEvidence]],
        ] = {}
        self._version_fingerprints: dict[tuple[str, str, str, int], frozenset[str]] = {}
        self._lock = RLock()

    def ingest(
        self,
        profile: NormalizedIssuerInstrumentReference,
    ) -> IssuerReferenceEvidence | None:
        if not isinstance(profile, NormalizedIssuerInstrumentReference):
            raise TypeError("profile must be a NormalizedIssuerInstrumentReference")
        evidence = IssuerReferenceEvidence.from_profile(profile)
        version_key = _version_key(evidence)
        with self._lock:
            known = self._version_fingerprints.get(version_key)
            if known is not None and (len(known) != 1 or evidence.fingerprint not in known):
                raise ValueError(
                    "conflicting issuer-reference content for the same source, instrument, "
                    "source_profile_id, and profile_version"
                )
            retained_keys = sorted(
                {*self._profiles, profile.instrument}, key=lambda item: item.instrument_id
            )[: self._max_instruments]
            if profile.instrument not in retained_keys:
                return None
            for existing_key in tuple(self._profiles):
                if existing_key not in retained_keys:
                    del self._profiles[existing_key]
            if known is None:
                self._version_fingerprints[version_key] = frozenset({evidence.fingerprint})
            profiles = self._profiles.setdefault(profile.instrument, {})
            versions = profiles.setdefault(profile.source_profile_id, {})
            existing = versions.get(profile.profile_version)
            if existing is None:
                versions[profile.profile_version] = evidence
            elif existing.fingerprint != evidence.fingerprint:  # pragma: no cover - registry guards
                raise ValueError("conflicting issuer reference escaped the version registry")
            self._trim_versions(versions)
            self._trim_profiles(profiles)
            self._trim_empty_instruments()
            self._trim_version_identities()
            return self._profiles.get(profile.instrument, {}).get(profile.source_profile_id, {}).get(
                profile.profile_version
            )

    def snapshot(self, *, symbol: str | None = None) -> IssuerReferenceSnapshot:
        requested_symbol = canonical_symbol(symbol)
        with self._lock:
            profiles = tuple(
                sorted(
                    (
                        profile
                        for key, occurrences in self._profiles.items()
                        if requested_symbol is None or symbols_match(key.source_symbol, requested_symbol)
                        for versions in occurrences.values()
                        for profile in versions.values()
                    ),
                    key=issuer_reference_sort_key,
                )
            )
        return IssuerReferenceSnapshot(
            profiles=profiles,
            fingerprint=_snapshot_fingerprint(profiles),
        )

    def revision_history(
        self,
        instrument: IssuerInstrumentReferenceKey,
        source_profile_id: str,
    ) -> tuple[IssuerReferenceEvidence, ...]:
        if not isinstance(instrument, IssuerInstrumentReferenceKey):
            raise TypeError("instrument must be an IssuerInstrumentReferenceKey")
        profile_id = source_profile_id.strip()
        if not profile_id:
            raise ValueError("source_profile_id cannot be blank")
        with self._lock:
            profiles = tuple(self._profiles.get(instrument, {}).get(profile_id, {}).values())
        return tuple(sorted(profiles, key=issuer_reference_sort_key))

    @property
    def profile_count(self) -> int:
        with self._lock:
            return sum(
                len(versions)
                for profiles in self._profiles.values()
                for versions in profiles.values()
            )

    @property
    def instrument_count(self) -> int:
        with self._lock:
            return len(self._profiles)

    @property
    def version_identity_count(self) -> int:
        with self._lock:
            return len(self._version_fingerprints)

    def _trim_versions(self, versions: dict[int, IssuerReferenceEvidence]) -> None:
        keep = set(sorted(versions, reverse=True)[: self._max_versions_per_profile])
        for version in tuple(versions):
            if version not in keep:
                del versions[version]

    def _trim_profiles(self, profiles: dict[str, dict[int, IssuerReferenceEvidence]]) -> None:
        keep = {
            profile_id
            for profile_id, versions in sorted(
                profiles.items(), key=lambda item: _profile_retention_key(item[0], item[1]), reverse=True
            )[: self._max_profiles_per_instrument]
        }
        for profile_id in tuple(profiles):
            if profile_id not in keep:
                del profiles[profile_id]

    def _trim_empty_instruments(self) -> None:
        for instrument in tuple(self._profiles):
            if not self._profiles[instrument]:
                del self._profiles[instrument]

    def _trim_version_identities(self) -> None:
        if len(self._version_fingerprints) <= self._max_version_identities:
            return
        retained = {
            _version_key(profile)
            for profiles in self._profiles.values()
            for versions in profiles.values()
            for profile in versions.values()
        }
        historical_capacity = self._max_version_identities - len(retained)
        historical = sorted(key for key in self._version_fingerprints if key not in retained)
        allowed = retained | set(historical[:historical_capacity])
        for key in tuple(self._version_fingerprints):
            if key not in allowed:
                del self._version_fingerprints[key]


def latest_issuer_reference_versions(
    profiles: tuple[IssuerReferenceEvidence, ...],
) -> tuple[IssuerReferenceEvidence, ...]:
    """Select current retained profile versions per source occurrence deterministically."""
    latest: dict[tuple[str, str, str], IssuerReferenceEvidence] = {}
    for profile in profiles:
        key = (
            profile.provenance.source_id,
            profile.instrument.instrument_id,
            profile.source_profile_id,
        )
        existing = latest.get(key)
        if existing is None or _current_version_key(profile) > _current_version_key(existing):
            latest[key] = profile
    return tuple(sorted(latest.values(), key=issuer_reference_sort_key))


def issuer_reference_sort_key(
    profile: IssuerReferenceEvidence,
) -> tuple[str, str, str, int, str]:
    return (
        profile.instrument.instrument_id,
        profile.source_profile_id,
        profile.as_of.isoformat(),
        profile.profile_version,
        profile.profile_id,
    )


def _current_version_key(profile: IssuerReferenceEvidence) -> tuple[int, str, str]:
    return (profile.profile_version, profile.as_of.isoformat(), profile.profile_id)


def _version_key(profile: IssuerReferenceEvidence) -> tuple[str, str, str, int]:
    return (
        profile.provenance.source_id,
        profile.instrument.instrument_id,
        profile.source_profile_id,
        profile.profile_version,
    )


def _profile_retention_key(
    source_profile_id: str,
    versions: dict[int, IssuerReferenceEvidence],
) -> tuple[str, int, str]:
    exemplar = max(versions.values(), key=_current_version_key)
    return (exemplar.as_of.isoformat(), exemplar.profile_version, source_profile_id)


def _snapshot_fingerprint(profiles: tuple[IssuerReferenceEvidence, ...]) -> str:
    payload = [
        {"profile_id": item.profile_id, "fingerprint": item.fingerprint}
        for item in profiles
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _validate_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
