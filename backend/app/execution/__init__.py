"""Execution boundary exports without eager Pilot construction/imports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.execution.pilot import PilotApprovalError, PilotExecutionCoordinator

__all__ = ["PilotApprovalError", "PilotExecutionCoordinator"]


def __getattr__(name: str):
    """Preserve legacy exports while unrelated execution submodules stay isolated."""
    if name in __all__:
        from app.execution.pilot import PilotApprovalError, PilotExecutionCoordinator

        return {
            "PilotApprovalError": PilotApprovalError,
            "PilotExecutionCoordinator": PilotExecutionCoordinator,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
