import urllib.parse
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.guardrails.pii_redactor import PIIRedactor

SENSITIVE_FIELD_PATTERNS = (
    "ssn", "pin", "cvv", "cvc", "card", "password", "secret", "tax-id",
    "routing", "dob", "pan", "account", "passcode", "otp", "mfa", "token", "security_code"
)


def _sanitize_url(raw_url: str) -> str:
    """Scrub sensitive query parameters and apply PII redaction to URL."""
    if not raw_url:
        return raw_url
    try:
        parts = urllib.parse.urlsplit(raw_url)
        if parts.query:
            query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            sanitized_pairs = []
            for qk, qv in query_pairs:
                if any(s in qk.lower() for s in SENSITIVE_FIELD_PATTERNS):
                    sanitized_pairs.append((qk, "[REDACTED_SENSITIVE_INPUT]"))
                else:
                    sanitized_pairs.append((qk, PIIRedactor.redact_text(qv)))
            parts = parts._replace(query=urllib.parse.urlencode(sanitized_pairs))
            raw_url = urllib.parse.urlunsplit(parts)
    except Exception:
        pass
    return PIIRedactor.redact_text(raw_url)


class RecordedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    sequence: int = Field(ge=0)
    action_type: str = Field(min_length=1)
    target: Dict[str, Any] = Field(default_factory=dict)
    value: Optional[str] = None
    is_masked: bool = False
    timestamp_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_masking_and_constraints(self) -> "RecordedAction":
        """Enforce that actions flagged as sensitive have masked values and auto-detect sensitive targets (INV-27)."""
        target_name = str(self.target.get("name") or self.target.get("id") or "").lower()
        target_type = str(self.target.get("type") or "").lower()
        target_aria = str(self.target.get("aria-label") or self.target.get("placeholder") or self.target.get("autocomplete") or "").lower()
        target_extra = str(self.target.get("selector") or self.target.get("xpath") or self.target.get("data-testid") or self.target.get("title") or "").lower()
        sensitive_kw = SENSITIVE_FIELD_PATTERNS
        if target_type == "password" or any(k in target_name or k in target_aria or k in target_extra for k in sensitive_kw):
            object.__setattr__(self, "is_masked", True)

        if self.target:
            object.__setattr__(self, "target", PIIRedactor.redact_dict(self.target))

        if self.is_masked and self.value:
            if not (self.value.startswith("[REDACTED") or self.value in ("[MASKED]", "***")):
                object.__setattr__(self, "value", "[REDACTED_SENSITIVE_INPUT]")
        elif self.value:
            object.__setattr__(self, "value", PIIRedactor.redact_text(self.value))
        return self

    def __repr__(self) -> str:
        safe_val = "[REDACTED_SENSITIVE_INPUT]" if self.is_masked else (self.value or "")
        return f"RecordedAction(seq={self.sequence}, type='{self.action_type}', value='{safe_val}')"


class StateDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    url_changed: bool = False
    pre_url: str = ""
    post_url: str = ""
    elements_added_count: int = Field(default=0, ge=0)
    elements_removed_count: int = Field(default=0, ge=0)
    form_fields_changed: Dict[str, str] = Field(default_factory=dict)
    summary: str = ""

    @model_validator(mode="after")
    def scrub_state_delta(self) -> "StateDelta":
        """Scrub sensitive keys in form_fields_changed and sanitize URL query params (INV-27, INV-32)."""
        if self.pre_url:
            object.__setattr__(self, "pre_url", _sanitize_url(self.pre_url))
        if self.post_url:
            object.__setattr__(self, "post_url", _sanitize_url(self.post_url))
        if self.summary:
            object.__setattr__(self, "summary", PIIRedactor.redact_text(self.summary))
        scrubbed = {}
        for k, v in self.form_fields_changed.items():
            k_lower = k.lower()
            if any(s in k_lower for s in SENSITIVE_FIELD_PATTERNS):
                scrubbed[PIIRedactor.redact_text(k)] = "[REDACTED_SENSITIVE_INPUT]"
            else:
                scrubbed[PIIRedactor.redact_text(k)] = PIIRedactor.redact_text(str(v))
        object.__setattr__(self, "form_fields_changed", scrubbed)
        return self


class HumanInterventionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    intervention_id: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    trigger_context: Dict[str, Any] = Field(default_factory=dict)
    pre_state: Dict[str, Any] = Field(default_factory=dict)
    recorded_actions: List[RecordedAction] = Field(default_factory=list)
    post_state: Dict[str, Any] = Field(default_factory=dict)
    state_delta: Optional[StateDelta] = None
    audit_metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def scrub_states(self) -> "HumanInterventionRecord":
        """Recursively scrub residual PII in state snapshots and trigger context (INV-27, INV-32)."""
        if self.trigger_context:
            object.__setattr__(self, "trigger_context", PIIRedactor.redact_dict(self.trigger_context))
        if self.pre_state:
            object.__setattr__(self, "pre_state", PIIRedactor.redact_dict(self.pre_state))
        if self.post_state:
            object.__setattr__(self, "post_state", PIIRedactor.redact_dict(self.post_state))
        if self.audit_metadata:
            object.__setattr__(self, "audit_metadata", PIIRedactor.redact_dict(self.audit_metadata))
        return self

    def __repr__(self) -> str:
        return f"HumanInterventionRecord(id='{self.intervention_id}', cap='{self.capability_id}', actions={len(self.recorded_actions)})"


