"""Unit tests for Capability Artifact Compiler."""

from src.discovery.compiler import CapabilityCompiler
from src.schemas.artifact import LocatorStrategyType, RiskLevel


def test_compiler_trace_pruning_and_parameterization() -> None:
    """Verify compiler prunes noops/finish and parameterizes input values."""
    raw_steps = [
        {
            "step_num": 1,
            "action": "navigate",
            "target_description": "Initial navigation",
            "value": "http://127.0.0.1:8123/servicing/lookup",
        },
        {
            "step_num": 2,
            "action": "type_text",
            "target_description": "Member ID input box",
            "value": "1042",
            "label_text": "Member ID:",
        },
        {
            "step_num": 3,
            "action": "click",
            "target_description": "Lookup Member button",
            "expected_feedback": "Regular Savings",
            "checkpoint_rule": {
                "warning_pattern": "Warning: No member found",
                "outcome_code": "MEMBER_NOT_FOUND",
            },
        },
        {
            "step_num": 4,
            "action": "extract_data",
            "target_description": "Savings balance cell",
            "extraction_field": "savings_balance",
            "transform": "parse_currency",
        },
        {
            "step_num": 5,
            "action": "finish",
            "target_description": "Goal accomplished",
        },
    ]

    artifact = CapabilityCompiler.compile_trace(
        capability_id="lookup_savings_balance",
        description="Lookup member savings balance",
        target_app="HeritageCore",
        base_url_pattern="http://127.0.0.1:8123/*",
        raw_steps=raw_steps,
        known_inputs={"member_id": "1042"},
    )

    # 1. Step 5 'finish' must be pruned
    assert len(artifact.steps) == 4

    # 2. Parameterization of member_id
    type_step = artifact.steps[1]
    assert type_step.value_binding == "$input.member_id"
    assert "member_id" in artifact.interface.inputs
    assert artifact.interface.inputs["member_id"].type == "string"

    # 3. Checkpoint synthesis
    click_step = artifact.steps[2]
    assert click_step.checkpoint is not None
    assert len(click_step.checkpoint.branches) == 2
    assert click_step.checkpoint.branches[0].outcome_code == "MEMBER_NOT_FOUND"

    # 4. Multi-tier locator cascade validation
    assert type_step.locator is not None
    strategies = [t.strategy for t in type_step.locator.tiers]
    assert LocatorStrategyType.ROLE_AND_NAME in strategies
    assert LocatorStrategyType.LABEL_PROXIMITY in strategies

    # 5. Output extraction
    extract_step = artifact.steps[3]
    assert len(extract_step.extractions) == 1
    assert extract_step.extractions[0].output_field == "savings_balance"
    assert extract_step.extractions[0].transform == "parse_currency"
    assert "savings_balance" in artifact.interface.outputs

    # 6. Risk Level
    assert artifact.metadata.risk_level == RiskLevel.SAFE_IDEMPOTENT_WRITE
