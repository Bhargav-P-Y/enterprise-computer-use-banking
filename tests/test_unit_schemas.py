"""Level 1 Unit Tests: Capability Artifact and Execution Schemas."""

import pytest
from pydantic import ValidationError

from src.schemas.artifact import (
    ActionType,
    CapabilityArtifact,
    CapabilityInterface,
    CapabilityMetadata,
    CapabilityStep,
    CheckpointBranch,
    LocatorCascade,
    LocatorStrategyType,
    LocatorTier,
    ParameterProperty,
    RiskLevel,
    StepCheckpoint,
)
from src.schemas.execution import ReplayResult, ReplayStatus


def create_sample_artifact() -> CapabilityArtifact:
    """Helper factory returning a canonical valid CapabilityArtifact."""
    return CapabilityArtifact(
        schema_version="1.0.0",
        metadata=CapabilityMetadata(
            capability_id="lookup_savings_balance",
            version="1.0.0",
            description="Searches member and extracts savings balance",
            target_app="HeritageCoreBanking",
            base_url_pattern="http://localhost:8000/servicing/*",
            risk_level=RiskLevel.SAFE_READ,
            created_at_utc="2026-09-18T10:00:00Z",
        ),
        interface=CapabilityInterface(
            inputs={
                "member_id": ParameterProperty(
                    type="string", description="6-digit member account ID", pattern=r"^[0-9]{4,8}$"
                )
            },
            required_inputs=["member_id"],
            outputs={
                "savings_balance": ParameterProperty(type="number", description="Current balance")
            },
            required_outputs=["savings_balance"],
        ),
        steps=[
            CapabilityStep(
                step_id="step_1",
                description="Navigate to lookup page",
                action=ActionType.NAVIGATE,
                value_binding="http://localhost:8000/servicing/lookup",
            ),
            CapabilityStep(
                step_id="step_2",
                description="Type member ID into search input",
                action=ActionType.TYPE_TEXT,
                locator=LocatorCascade(
                    tiers=[
                        LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role="textbox", name="Member ID"),
                        LocatorTier(strategy=LocatorStrategyType.LABEL_PROXIMITY, label_text="Member ID:", direction="right"),
                    ],
                    robustness_rationale="Uses label proximity as fallback for ASP dynamic ID",
                    target_frame="app_main_frame",
                ),
                value_binding="$input.member_id",
            ),
            CapabilityStep(
                step_id="step_3",
                description="Click lookup button and check result",
                action=ActionType.CLICK,
                locator=LocatorCascade(
                    tiers=[
                        LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role="button", name="Lookup Member"),
                    ],
                    robustness_rationale="Matches submit button by accessible name",
                    target_frame="app_main_frame",
                ),
                checkpoint=StepCheckpoint(
                    branches=[
                        CheckpointBranch(
                            condition_type="text_visible",
                            pattern="Warning: No member found",
                            outcome_category="business_outcome",
                            outcome_code="MEMBER_NOT_FOUND",
                            message="Member does not exist in institution registry.",
                        ),
                        CheckpointBranch(
                            condition_type="element_visible",
                            pattern="ctl00_Main_gvMembers",
                            outcome_category="continue",
                        ),
                    ]
                ),
            ),
        ],
    )


def test_capability_artifact_valid_construction() -> None:
    """Verify complete CapabilityArtifact instantiation and round-trip JSON serialization."""
    artifact = create_sample_artifact()
    json_str = artifact.model_dump_json(indent=2)
    assert "lookup_savings_balance" in json_str
    assert "MEMBER_NOT_FOUND" in json_str

    # Round trip reload
    reloaded = CapabilityArtifact.model_validate_json(json_str)
    assert reloaded.metadata.capability_id == "lookup_savings_balance"
    assert len(reloaded.steps) == 3


def test_capability_artifact_invalid_fails() -> None:
    """Verify validation error when missing required fields."""
    with pytest.raises(ValidationError):
        # Missing required steps and metadata
        CapabilityArtifact.model_validate({})


def test_replay_result_contract() -> None:
    """Verify ReplayResult model contracts for Success, Business Outcome, and Failure."""
    res_success = ReplayResult(
        status=ReplayStatus.SUCCESS,
        capability_id="lookup_savings_balance",
        duration_ms=450,
        data={"savings_balance": 4850.25},
    )
    assert res_success.status == ReplayStatus.SUCCESS
    assert res_success.data["savings_balance"] == 4850.25

    res_business = ReplayResult(
        status=ReplayStatus.BUSINESS_OUTCOME,
        capability_id="lookup_savings_balance",
        duration_ms=210,
        business_outcome_code="MEMBER_NOT_FOUND",
        message="Member ID 9999 does not exist",
    )
    assert res_business.status == ReplayStatus.BUSINESS_OUTCOME
    assert res_business.business_outcome_code == "MEMBER_NOT_FOUND"

    res_failure = ReplayResult(
        status=ReplayStatus.HARD_FAILURE,
        capability_id="lookup_savings_balance",
        duration_ms=5000,
        failure_context={"failed_step": "step_2", "reason": "Element not found"},
    )
    assert res_failure.status == ReplayStatus.HARD_FAILURE
