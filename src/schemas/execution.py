"""Execution and Replay Result Schemas conforming to Tri-Partite Error Taxonomy."""

from enum import Enum
from typing import Any, Dict, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"


class FailureContext(BaseModel):
    """Detailed contextual diagnostic evidence for hard failures."""
    model_config = ConfigDict(extra="allow")

    step_id: Optional[str] = None
    failed_step: Optional[str] = None
    failed_step_id: Optional[str] = None
    action: Optional[str] = None
    reason: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    screenshot_path: Optional[str] = None
    evidence_paths: Optional[Dict[str, Any]] = None

    def __contains__(self, key: str) -> bool:
        if key in self.__class__.model_fields:
            return True
        extra = getattr(self, "__pydantic_extra__", None)
        return bool(extra and key in extra)

    def __getitem__(self, key: str) -> Any:
        if key in self.__class__.model_fields:
            return getattr(self, key)
        extra = getattr(self, "__pydantic_extra__", None)
        if extra and key in extra:
            return extra[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


class ReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    strict_mode: bool = True
    session_id: Optional[str] = None

    def __repr__(self) -> str:
        sensitive_keys = (
            "ssn", "dob", "password", "pin", "secret", "account",
            "routing", "card", "cvv", "pan", "token", "auth"
        )
        def _mask_val(v: Any, depth: int = 0) -> Any:
            if depth > 5:
                return "..."
            if isinstance(v, dict):
                return {k2: ("***" if any(s in k2.lower() for s in sensitive_keys) else _mask_val(v2, depth + 1)) for k2, v2 in v.items()}
            elif isinstance(v, (list, tuple, set)):
                return [_mask_val(item, depth + 1) for item in v]
            return v

        safe_params = {
            k: ("***" if any(s in k.lower() for s in sensitive_keys) else _mask_val(v))
            for k, v in self.parameters.items()
        }
        return f"ReplayInput(capability_id='{self.capability_id}', parameters={safe_params}, strict_mode={self.strict_mode})"


class ReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ReplayStatus
    capability_id: str
    duration_ms: int = Field(default=0, ge=0)
    data: Optional[Dict[str, Any]] = None              # Populated on SUCCESS
    business_outcome_code: Optional[str] = None       # e.g., "MEMBER_NOT_FOUND"
    message: Optional[str] = None
    failure_context: Optional[Union[FailureContext, Dict[str, Any]]] = None  # Step, expected, observed, evidence paths

    @model_validator(mode="after")
    def validate_tripartite_mutual_exclusivity(self) -> "ReplayResult":
        """Enforce strict mutual exclusivity across tri-partite error taxonomy (INV-20, INV-21)."""
        if self.status == ReplayStatus.SUCCESS:
            if self.business_outcome_code is not None:
                raise ValueError("business_outcome_code must not be set when status is SUCCESS")
            if self.failure_context is not None:
                raise ValueError("failure_context must not be set when status is SUCCESS")
        elif self.status == ReplayStatus.BUSINESS_OUTCOME:
            if not self.business_outcome_code or not self.business_outcome_code.strip():
                raise ValueError("business_outcome_code must be provided when status is BUSINESS_OUTCOME")
            if self.failure_context is not None:
                raise ValueError("failure_context must not be set when status is BUSINESS_OUTCOME (INV-21)")
        elif self.status == ReplayStatus.HARD_FAILURE:
            if self.business_outcome_code is not None:
                raise ValueError("business_outcome_code must not be set when status is HARD_FAILURE")
        return self

    def __repr__(self) -> str:
        return f"ReplayResult(status={self.status.value}, capability_id='{self.capability_id}', duration_ms={self.duration_ms})"

