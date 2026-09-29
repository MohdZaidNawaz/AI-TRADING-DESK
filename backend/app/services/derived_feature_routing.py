"""Specialist routing for immutable derived-feature snapshots."""

from __future__ import annotations

from app.evidence.router import NINE_SPECIALIST_IDS
from app.models.derived_features import (
    DerivedFeatureSnapshot,
    DerivedMarketFeature,
    FeatureType,
)


_CHARTIST_FEATURES = frozenset(
    {
        FeatureType.BAR_RANGE,
        FeatureType.AVERAGE_RANGE,
        FeatureType.SMA,
        FeatureType.ROC,
    }
)
_ORACLE_FEATURES = frozenset(
    {
        FeatureType.SIMPLE_RETURN,
        FeatureType.LOG_RETURN,
        FeatureType.ROLLING_STD,
        FeatureType.ROC,
    }
)


class DerivedFeatureRouter:
    """Route calculated values separately from raw evidence."""

    def route(
        self,
        *,
        agent_id: str,
        snapshot: DerivedFeatureSnapshot,
    ) -> tuple[DerivedMarketFeature, ...]:
        if agent_id not in NINE_SPECIALIST_IDS:
            raise ValueError(f"Unknown FRIDAY specialist ID: {agent_id}")
        if not isinstance(snapshot, DerivedFeatureSnapshot):
            raise TypeError("snapshot must be a DerivedFeatureSnapshot")

        if agent_id == "chartist_technician":
            allowed = _CHARTIST_FEATURES
        elif agent_id == "oracle_quant":
            allowed = _ORACLE_FEATURES
        else:
            return ()
        return tuple(
            feature for feature in snapshot.features if feature.feature_type in allowed
        )
