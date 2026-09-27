"""Pydantic v2 Capability Artifact Schema and Contract Definitions."""

from datetime import datetime
from enum import Enum
import re
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RiskLevel(str, Enum):
    SAFE_READ = "safe_read_only"
    SAFE_IDEMPOTENT_WRITE = "safe_idempotent_write"
    RISKY_IRREVERSIBLE = "risky_irreversible"


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE_TEXT = "type_text"
    SELECT_OPTION = "select_option"
    EXTRACT_DATA = "extract_data"
    WAIT_FOR_STATE = "wait_for_state"


class LocatorStrategyType(str, Enum):
    ROLE_AND_NAME = "role_and_name"          # Tier 1: Accessibility tree
    LABEL_PROXIMITY = "label_proximity"      # Tier 2: Spatial label relation
    STRUCTURAL_XPATH = "structural_xpath"    # Tier 3: Invariant DOM structure
    COORDINATE_RATIO = "coordinate_ratio"    # Tier 4: Relative bbox fallback


class LocatorTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: LocatorStrategyType
    role: Optional[str] = None
    name: Optional[str] = None
    label_text: Optional[str] = None
    direction: Optional[Literal["right", "below", "inside"]] = "right"
    xpath: Optional[str] = None
    coords_ratio: Optional[List[float]] = None  # [x_ratio, y_ratio] (0.0 to 1.0)

    @field_validator("coords_ratio")
    @classmethod
    def validate_coords_ratio(cls, v: Optional[List[float]]) -> Optional[List[float]]:
        if v is not None:
            if len(v) != 2:
                raise ValueError("coords_ratio must be a 2-element list [x, y]")
            if not all(0.0 <= x <= 1.0 for x in v):
                raise ValueError("coords_ratio values must be between 0.0 and 1.0")
        return v

    @field_validator("xpath")
    @classmethod
    def validate_xpath_syntax(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v_clean = v.strip()
            # Disallow invalid parenthesized tags per INV-11 (e.g. //(input) or (/input)[1])
            if re.search(r"//\s*\(", v_clean) or re.search(r"^\s*\(/", v_clean):
                raise ValueError(f"Invalid XPath syntax: parenthesized tags are forbidden under INV-11: '{v}'")
        return v

    @model_validator(mode="after")
    def validate_strategy_attributes(self) -> "LocatorTier":
        if self.strategy == LocatorStrategyType.STRUCTURAL_XPATH and not (self.xpath and self.xpath.strip()):
            raise ValueError("xpath must be provided when strategy is STRUCTURAL_XPATH")
        if self.strategy == LocatorStrategyType.COORDINATE_RATIO and not self.coords_ratio:
            raise ValueError("coords_ratio must be provided when strategy is COORDINATE_RATIO")
        if self.strategy == LocatorStrategyType.LABEL_PROXIMITY and not (self.label_text and self.label_text.strip()):
            raise ValueError("label_text must be provided when strategy is LABEL_PROXIMITY")
        if self.strategy == LocatorStrategyType.ROLE_AND_NAME and not ((self.role and self.role.strip()) or (self.name and self.name.strip())):
            raise ValueError("role or name must be provided when strategy is ROLE_AND_NAME")
        return self


class LocatorCascade(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: List[LocatorTier] = Field(..., min_length=1)
    robustness_rationale: str
    target_frame: Optional[Union[str, List[str]]] = None  # if located inside an iframe or nested frameset (INV-09)

    @field_validator("target_frame")
    @classmethod
    def validate_target_frame(cls, v: Optional[Union[str, List[str]]]) -> Optional[Union[str, List[str]]]:
        if v is not None:
            if isinstance(v, str):
                v_clean = v.strip()
                if not v_clean:
                    raise ValueError("target_frame string cannot be empty or whitespace")
                return v_clean
            elif isinstance(v, list):
                if not v:
                    raise ValueError("target_frame list cannot be empty")
                cleaned = []
                for item in v:
                    if not isinstance(item, str) or not item.strip():
                        raise ValueError("target_frame list elements must be non-empty strings")
                    cleaned.append(item.strip())
                return cleaned
        return v

    @model_validator(mode="after")
    def validate_tier_ordering(self) -> "LocatorCascade":
        order_map = {
            LocatorStrategyType.ROLE_AND_NAME: 1,
            LocatorStrategyType.LABEL_PROXIMITY: 2,
            LocatorStrategyType.STRUCTURAL_XPATH: 3,
            LocatorStrategyType.COORDINATE_RATIO: 4,
        }
        seen = set()
        last_rank = 0
        for tier in self.tiers:
            if tier.strategy in seen:
                raise ValueError(f"Duplicate strategy '{tier.strategy}' in LocatorCascade")
            seen.add(tier.strategy)
            rank = order_map.get(tier.strategy, 99)
            if rank < last_rank:
                raise ValueError("LocatorCascade tiers must follow strict progression (INV-10): Tier 1 -> Tier 2 -> Tier 3 -> Tier 4")
            last_rank = rank
        return self


class CheckpointBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_type: Literal["text_visible", "element_visible", "url_matches"]
    pattern: str
    outcome_category: Literal["continue", "business_outcome", "hard_failure"]
    outcome_code: Optional[str] = None  # e.g., "MEMBER_NOT_FOUND"
    message: Optional[str] = None

    @model_validator(mode="after")
    def validate_branch_integrity(self) -> "CheckpointBranch":
        if self.outcome_category == "business_outcome":
            if not self.outcome_code or not self.outcome_code.strip():
                raise ValueError("outcome_code must be provided when outcome_category is 'business_outcome'")
        # Scope regex validation strictly to text and url conditions; element_visible uses DOM selectors (INV-18)
        if self.condition_type in ("text_visible", "url_matches"):
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern for {self.condition_type} in CheckpointBranch: '{self.pattern}': {e}")
        return self


class StepCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait_condition: Literal["dom_content_loaded", "network_idle", "element_attached"] = "dom_content_loaded"
    timeout_ms: int = Field(default=5000, ge=100, le=30000)
    assertions: Optional[List[str]] = None
    branches: Optional[List[CheckpointBranch]] = None


class StepExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_field: str
    locator: LocatorCascade
    transform: Literal["raw_text", "parse_currency", "trim_string", "extract_digits"] = "raw_text"


class CapabilityStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str
    action: ActionType
    locator: Optional[LocatorCascade] = None
    value_binding: Optional[str] = None  # e.g. "$input.member_id" or raw string
    is_sensitive: bool = False
    target_frame: Optional[Union[str, List[str]]] = None  # Sub-frame name, selector, id, or nested hierarchy (INV-09)
    extractions: Optional[List[StepExtraction]] = None
    checkpoint: Optional[StepCheckpoint] = None

    @model_validator(mode="after")
    def validate_action_locator(self) -> "CapabilityStep":
        if self.action in (ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.SELECT_OPTION):
            if not self.locator:
                raise ValueError(f"Action '{self.action}' requires a locator cascade")
        if self.action in (ActionType.TYPE_TEXT, ActionType.SELECT_OPTION):
            if not self.value_binding or not self.value_binding.strip():
                raise ValueError(f"Action '{self.action}' requires a non-empty value_binding")
        if self.action == ActionType.EXTRACT_DATA:
            if not self.extractions:
                raise ValueError("extractions must be provided when action is EXTRACT_DATA")
        if self.action == ActionType.NAVIGATE and (not self.value_binding or not self.value_binding.strip()):
            raise ValueError("Action 'navigate' requires a non-empty value_binding URL")
        if self.action == ActionType.WAIT_FOR_STATE:
            if not self.locator and not self.checkpoint:
                raise ValueError("Action 'wait_for_state' requires either a locator or a checkpoint to evaluate state")
        return self


class ParameterProperty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "number", "integer", "boolean"]
    description: str
    pattern: Optional[str] = None
    default: Optional[Any] = None
    is_secret: bool = False

    @field_validator("pattern")
    @classmethod
    def validate_regex_pattern(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern '{v}': {e}")
        return v

    @model_validator(mode="after")
    def validate_secret_default_and_types(self) -> "ParameterProperty":
        if self.is_secret and self.default is not None:
            raise ValueError("Default values are forbidden for secret parameters (INV-28)")
        if self.default is not None:
            if self.type == "integer" and (not isinstance(self.default, int) or isinstance(self.default, bool)):
                raise ValueError(f"Default value '{self.default}' does not match integer type")
            elif self.type == "number" and (not isinstance(self.default, (int, float)) or isinstance(self.default, bool)):
                raise ValueError(f"Default value '{self.default}' does not match number type")
            elif self.type == "boolean" and not isinstance(self.default, bool):
                raise ValueError(f"Default value '{self.default}' does not match boolean type")
            elif self.type == "string" and not isinstance(self.default, str):
                raise ValueError(f"Default value '{self.default}' does not match string type")
        return self


class CapabilityInterface(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: Dict[str, ParameterProperty]
    required_inputs: List[str]
    outputs: Dict[str, ParameterProperty]
    required_outputs: List[str]

    @model_validator(mode="after")
    def validate_interface_keys(self) -> "CapabilityInterface":
        missing_req_inputs = set(self.required_inputs) - set(self.inputs.keys())
        if missing_req_inputs:
            raise ValueError(f"required_inputs contains undeclared inputs: {missing_req_inputs}")
        missing_req_outputs = set(self.required_outputs) - set(self.outputs.keys())
        if missing_req_outputs:
            raise ValueError(f"required_outputs contains undeclared outputs: {missing_req_outputs}")
        return self


class CapabilityMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    version: str = "1.0.0"
    description: str
    target_app: str
    base_url_pattern: str
    surface_type: Literal["legacy_web", "modern_web", "desktop"] = "legacy_web"
    risk_level: RiskLevel = RiskLevel.SAFE_READ
    created_at_utc: str

    @field_validator("base_url_pattern")
    @classmethod
    def validate_base_url_pattern(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"Invalid base_url_pattern regex '{v}': {e}")
        return v

    @field_validator("created_at_utc")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        try:
            # Validate ISO-8601 UTC format (INV-18)
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None or dt.utcoffset() is None or dt.utcoffset().total_seconds() != 0:
                raise ValueError("created_at_utc must have an explicit zero UTC offset")
        except Exception:
            raise ValueError(f"created_at_utc must be valid ISO-8601 UTC timestamp: {v}")
        return v


class CapabilityArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    metadata: CapabilityMetadata
    interface: CapabilityInterface
    guardrails_ref: Dict[str, Any] = Field(default_factory=dict)
    steps: List[CapabilityStep] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_step_bindings_against_interface(self) -> "CapabilityArtifact":
        # Validate unique step_ids across steps list (INV-18)
        step_ids = [s.step_id for s in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("All step_ids within CapabilityArtifact must be strictly unique to prevent trace ambiguity (INV-18)")

        updated_steps = []
        for step in self.steps:
            new_step = step
            if step.value_binding and "$input." in step.value_binding:
                matches = re.findall(r"\$input\.([a-zA-Z0-9_]+)", step.value_binding)
                for param_name in matches:
                    if param_name not in self.interface.inputs:
                        raise ValueError(f"Step '{step.step_id}' references undeclared input parameter: '{param_name}'")
                    # Propagate secret sensitivity idempotently without in-place mutation (INV-28, INV-31)
                    if self.interface.inputs[param_name].is_secret and not new_step.is_sensitive:
                        new_step = new_step.model_copy(update={"is_sensitive": True})
            if step.extractions:
                for ext in step.extractions:
                    if ext.output_field not in self.interface.outputs:
                        raise ValueError(f"Step '{step.step_id}' extraction references undeclared output field: '{ext.output_field}'")
            updated_steps.append(new_step)
        self.steps = updated_steps
        return self
