"""Immutable provider-neutral execution-boundary contracts; no adapter behavior."""
from __future__ import annotations
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
from pydantic import ConfigDict, Field, FiniteFloat, computed_field, field_validator
from app.models.events import AssetClass
from app.models.proposals import TradeDirection
from app.models.schemas import APIModel

class ExecutionModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
class ExecutionStatus(str, Enum):
    CREATED="created"; VALIDATED="validated"; READY_FOR_ADAPTER="ready_for_adapter"; NOT_EXECUTED="not_executed"; ADAPTER_UNAVAILABLE="adapter_unavailable"; EXPIRED="expired"; FAILED="failed"; DRY_RUN="dry_run"
class ExecutionIntent(ExecutionModel):
    proposal_id:str; proposal_fingerprint:str=Field(min_length=64,max_length=64); analysis_id:str; symbol:str; asset_class:AssetClass; direction:TradeDirection
    requested_entry:FiniteFloat; stop_loss:FiniteFloat; take_profit:FiniteFloat
    requested_risk_fraction:FiniteFloat|None=None; quantity:FiniteFloat|None=None; order_type:str|None=None; filling_mode:str|None=None; time_in_force:str|None=None
    approval_id:str; policy_id:str; policy_fingerprint:str=Field(min_length=64,max_length=64); assessment_id:str; assessment_fingerprint:str=Field(min_length=64,max_length=64); evidence_fingerprint:str=Field(min_length=64,max_length=64)
    account_id:str|None=None; portfolio_snapshot_id:str|None=None; portfolio_snapshot_fingerprint:str|None=Field(default=None,min_length=64,max_length=64); created_at:datetime; expires_at:datetime; status:ExecutionStatus=ExecutionStatus.READY_FOR_ADAPTER
    @field_validator("created_at","expires_at")
    @classmethod
    def utc(cls,v:datetime)->datetime:
        if v.tzinfo is None or v.utcoffset() is None: raise ValueError("execution timestamps require timezone")
        return v.astimezone(UTC)
    @computed_field(return_type=str)
    @property
    def execution_intent_id(self)->str:
        return "execution-intent-"+_fp(self._identity())
    @computed_field(return_type=str)
    @property
    def fingerprint(self)->str: return _fp({**self._identity(),"status":self.status.value})
    def _identity(self)->dict[str,object]:
        return {"proposal_id":self.proposal_id,"proposal_fingerprint":self.proposal_fingerprint,"analysis_id":self.analysis_id,"symbol":self.symbol,"direction":self.direction.value,"requested_entry":self.requested_entry,"stop_loss":self.stop_loss,"take_profit":self.take_profit,"requested_risk_fraction":self.requested_risk_fraction,"quantity":self.quantity,"order_type":self.order_type,"filling_mode":self.filling_mode,"time_in_force":self.time_in_force,"approval_id":self.approval_id,"policy_id":self.policy_id,"policy_fingerprint":self.policy_fingerprint,"assessment_id":self.assessment_id,"assessment_fingerprint":self.assessment_fingerprint,"evidence_fingerprint":self.evidence_fingerprint,"account_id":self.account_id,"portfolio_snapshot_id":self.portfolio_snapshot_id,"portfolio_snapshot_fingerprint":self.portfolio_snapshot_fingerprint,"expires_at":self.expires_at.isoformat()}
class ExecutionResult(ExecutionModel):
    intent_id:str; proposal_id:str; status:ExecutionStatus; reason_codes:tuple[str,...]=(); evaluated_at:datetime; adapter_status:str; safe_request:tuple[tuple[str,str],...]=()
    @field_validator("evaluated_at")
    @classmethod
    def utc(cls,v:datetime)->datetime:
        if v.tzinfo is None or v.utcoffset() is None: raise ValueError("execution timestamps require timezone")
        return v.astimezone(UTC)
def _fp(value:object)->str: return sha256(json.dumps(value,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
