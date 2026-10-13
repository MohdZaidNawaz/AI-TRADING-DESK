from functools import lru_cache

from app.agents import FridayCore, create_friday_core
from app.config import get_shared_authority_db_path
from app.execution.shared_authority_store import SharedAuthorityStore
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
    return create_friday_core(sentinel_gate=SentinelRiskGate(registry=approval_registry))
