"""Bounded thread-safe process-local intent store; restart fails closed."""
from __future__ import annotations
from threading import RLock
from app.models.execution import ExecutionIntent
class ExecutionIntentStore:
    def __init__(self,max_records:int=1000)->None:
        if max_records<1: raise ValueError("max_records must be positive")
        self._max=max_records; self._items:dict[str,ExecutionIntent]={}; self._lock=RLock()
    def put(self,intent:ExecutionIntent)->ExecutionIntent:
        with self._lock:
            existing=self._items.get(intent.execution_intent_id)
            if existing is not None:
                if existing.fingerprint!=intent.fingerprint: raise ValueError("conflicting_execution_intent")
                return existing
            self._items[intent.execution_intent_id]=intent
            for key in sorted(self._items,key=lambda x:(self._items[x].expires_at,x))[:max(0,len(self._items)-self._max)]: self._items.pop(key)
            return intent
    @property
    def count(self)->int:
        with self._lock:return len(self._items)
