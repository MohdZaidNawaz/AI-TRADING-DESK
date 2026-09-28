from functools import lru_cache

from app.agents import FridayCore, create_friday_core


@lru_cache
def get_friday_core() -> FridayCore:
    """Return the process-local FRIDAY Core instance used by API entry points."""
    return create_friday_core()
