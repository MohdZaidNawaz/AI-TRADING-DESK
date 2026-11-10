from functools import lru_cache

from app.agents import FridayCore, create_friday_core
from app.catalogue.store import (
    InstrumentCatalogueSchemaError,
    read_catalogue_snapshot,
)
from app.config import get_shared_authority_db_path
from app.execution.shared_authority_store import SharedAuthorityStore
from app.services.instrument_catalogue import DEFAULT_CATALOGUE_CACHE_PATH
from app.services.research_requests import ResearchRequestService
from app.services.trade_proposal_workflow import ApprovalRegistry, SentinelRiskGate


@lru_cache
def get_friday_core() -> FridayCore:
    """Return the application-composed FRIDAY Core and durable approval owner.

    Store initialization performs the fail-closed structural verification before
    the Sentinel gate can issue an approval. There is no process-local approval
    fallback when the configured shared SQLite database is unavailable.
    """
    approval_store = SharedAuthorityStore(get_shared_authority_db_path())
    approval_registry = ApprovalRegistry(authority_store=approval_store)
    try:
        catalogue_entries = read_catalogue_snapshot(DEFAULT_CATALOGUE_CACHE_PATH)
    except (InstrumentCatalogueSchemaError, OSError, ValueError):
        catalogue_entries = ()
    return create_friday_core(
        sentinel_gate=SentinelRiskGate(registry=approval_registry),
        research_request_service=ResearchRequestService(
            catalogue_entries=catalogue_entries
        ),
    )
