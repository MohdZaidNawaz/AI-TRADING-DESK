"""Thread-safe bounded storage for provider-neutral analyst-estimate evidence."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from threading import RLock

from app.evidence.models import (
    EstimateObservationEvidence,
    EstimateSeriesEvidence,
    EstimateSeriesSnapshot,
    canonical_symbol,
    symbols_match,
)
from app.models.estimates import (
    EstimateReferencePeriod,
    EstimateSeriesKey,
    NormalizedEstimateObservation,
)


class EstimateStore:
    """Retain bounded source-reported estimates and their recent vintages.

    Full observations are bounded by series, target-period occurrence, and
    retained vintage.  The separate bounded identity registry keeps only
    immutable content fingerprints for post-eviction conflict detection.
    """

    def __init__(
        self,
        *,
        max_series: int = 20,
        max_occurrences_per_series: int = 500,
        max_vintages_per_occurrence: int = 5,
        max_version_identities: int | None = None,
    ) -> None:
        _validate_limit(max_series, "max_series")
        _validate_limit(max_occurrences_per_series, "max_occurrences_per_series")
        _validate_limit(max_vintages_per_occurrence, "max_vintages_per_occurrence")
        full_capacity = max_series * max_occurrences_per_series * max_vintages_per_occurrence
        identity_limit = full_capacity * 2 if max_version_identities is None else max_version_identities
        _validate_limit(identity_limit, "max_version_identities")
        if identity_limit < full_capacity:
            raise ValueError("max_version_identities cannot be less than retained full capacity")

        self._max_series = max_series
        self._max_occurrences_per_series = max_occurrences_per_series
        self._max_vintages_per_occurrence = max_vintages_per_occurrence
        self._max_version_identities = identity_limit
        self._series: dict[
            EstimateSeriesKey,
            dict[str, dict[int, NormalizedEstimateObservation]],
        ] = {}
        self._version_fingerprints: dict[
            tuple[str, str, str, int],
            frozenset[str],
        ] = {}
        self._lock = RLock()

    def ingest(
        self,
        observation: NormalizedEstimateObservation,
    ) -> EstimateSeriesEvidence | None:
        """Stage one estimate observation while preserving revisions and conflicts."""
        if not isinstance(observation, NormalizedEstimateObservation):
            raise TypeError("observation must be a NormalizedEstimateObservation")

        key = observation.series_key
        version_key = _version_key(observation)
        with self._lock:
            known_fingerprints = self._version_fingerprints.get(version_key)
            if known_fingerprints is not None and (
                len(known_fingerprints) != 1
                or observation.fingerprint not in known_fingerprints
            ):
                raise ValueError(
                    "conflicting estimate-observation content for the same source, series, "
                    "reference period, and observation_version"
                )

            retained_keys = sorted(
                {*self._series.keys(), key},
                key=lambda item: item.series_id,
            )[: self._max_series]
            if key not in retained_keys:
                return None
            for existing_key in tuple(self._series):
                if existing_key not in retained_keys:
                    del self._series[existing_key]

            if known_fingerprints is None:
                self._version_fingerprints[version_key] = frozenset({observation.fingerprint})

            occurrences = self._series.setdefault(key, {})
            period_id = observation.reference_period.period_id
            vintages = occurrences.setdefault(period_id, {})
            existing = vintages.get(observation.observation_version)
            if existing is None:
                vintages[observation.observation_version] = observation
            elif existing.fingerprint != observation.fingerprint:  # pragma: no cover - registry guards
                raise ValueError("conflicting estimate observation escaped the version registry")

            self._trim_vintages(vintages)
            self._trim_occurrences(occurrences)
            self._trim_empty_series()
            self._trim_version_identities()
            retained = self._series.get(key, {}).get(period_id, {}).get(
                observation.observation_version
            )
            if retained is None:
                return None
            return self._build_series(key)

    def snapshot(self, *, symbol: str | None = None) -> EstimateSeriesSnapshot:
        """Return a deterministically ordered immutable view of retained estimates."""
        requested_symbol = canonical_symbol(symbol)
        with self._lock:
            series = tuple(
                sorted(
                    (
                        self._build_series(key)
                        for key in self._series
                        if requested_symbol is None or symbols_match(key.symbol, requested_symbol)
                    ),
                    key=lambda item: item.series_id,
                )
            )
        return EstimateSeriesSnapshot(
            series=series,
            fingerprint=_snapshot_fingerprint(series),
        )

    def get_series(self, key: EstimateSeriesKey) -> EstimateSeriesEvidence | None:
        if not isinstance(key, EstimateSeriesKey):
            raise TypeError("key must be an EstimateSeriesKey")
        with self._lock:
            return self._build_series(key) if key in self._series else None

    def current_observations(
        self,
        key: EstimateSeriesKey,
    ) -> tuple[EstimateObservationEvidence, ...]:
        series = self.get_series(key)
        return series.current_observations if series is not None else ()

    def vintage_history(
        self,
        key: EstimateSeriesKey,
        reference_period: EstimateReferencePeriod,
    ) -> tuple[EstimateObservationEvidence, ...]:
        if not isinstance(key, EstimateSeriesKey):
            raise TypeError("key must be an EstimateSeriesKey")
        if not isinstance(reference_period, EstimateReferencePeriod):
            raise TypeError("reference_period must be an EstimateReferencePeriod")
        with self._lock:
            observations = tuple(
                self._series.get(key, {})
                .get(reference_period.period_id, {})
                .values()
            )
        return tuple(
            EstimateObservationEvidence.from_observation(item)
            for item in sorted(observations, key=_observation_sort_key)
        )

    @property
    def series_count(self) -> int:
        with self._lock:
            return len(self._series)

    @property
    def observation_count(self) -> int:
        with self._lock:
            return sum(
                len(vintages)
                for occurrences in self._series.values()
                for vintages in occurrences.values()
            )

    @property
    def version_identity_count(self) -> int:
        """Include bounded post-eviction conflict-detection metadata."""
        with self._lock:
            return len(self._version_fingerprints)

    def _build_series(self, key: EstimateSeriesKey) -> EstimateSeriesEvidence:
        observations = tuple(
            observation
            for vintages in self._series[key].values()
            for observation in vintages.values()
        )
        return EstimateSeriesEvidence.from_observations(key, observations)

    def _trim_vintages(self, vintages: dict[int, NormalizedEstimateObservation]) -> None:
        retained_versions = set(
            sorted(vintages, reverse=True)[: self._max_vintages_per_occurrence]
        )
        for version in tuple(vintages):
            if version not in retained_versions:
                del vintages[version]

    def _trim_occurrences(
        self,
        occurrences: dict[str, dict[int, NormalizedEstimateObservation]],
    ) -> None:
        retained_periods = set(
            period_id
            for period_id, vintages in sorted(
                occurrences.items(),
                key=lambda item: _period_retention_key(item[0], item[1]),
                reverse=True,
            )[: self._max_occurrences_per_series]
        )
        for period_id in tuple(occurrences):
            if period_id not in retained_periods:
                del occurrences[period_id]

    def _trim_empty_series(self) -> None:
        for key in tuple(self._series):
            if not self._series[key]:
                del self._series[key]

    def _trim_version_identities(self) -> None:
        if len(self._version_fingerprints) <= self._max_version_identities:
            return
        retained_keys = {
            _version_key(observation)
            for occurrences in self._series.values()
            for vintages in occurrences.values()
            for observation in vintages.values()
        }
        historical_capacity = self._max_version_identities - len(retained_keys)
        historical_keys = sorted(
            key for key in self._version_fingerprints if key not in retained_keys
        )
        allowed = retained_keys | set(historical_keys[:historical_capacity])
        for key in tuple(self._version_fingerprints):
            if key not in allowed:
                del self._version_fingerprints[key]


def _version_key(
    observation: NormalizedEstimateObservation,
) -> tuple[str, str, str, int]:
    return (
        observation.source.source_id,
        observation.series_key.series_id,
        observation.reference_period.period_id,
        observation.observation_version,
    )


def _observation_sort_key(
    observation: NormalizedEstimateObservation,
) -> tuple[date, date, str, int, str]:
    return (
        observation.reference_period.start_date,
        observation.reference_period.end_date,
        observation.reference_period.period_id,
        observation.observation_version,
        observation.observation_id,
    )


def _period_retention_key(
    period_id: str,
    vintages: dict[int, NormalizedEstimateObservation],
) -> tuple[date, date, str]:
    exemplar = next(iter(vintages.values()))
    period = exemplar.reference_period
    return (period.end_date, period.start_date, period_id)


def _snapshot_fingerprint(series: tuple[EstimateSeriesEvidence, ...]) -> str:
    payload = [
        {"series_id": item.series_id, "fingerprint": item.fingerprint}
        for item in series
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _validate_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
