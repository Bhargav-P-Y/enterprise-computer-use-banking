"""Level 1 Unit Tests: Tri-Partite Outcome Classifier."""

from src.schemas.artifact import CheckpointBranch, StepCheckpoint
from src.schemas.execution import ReplayStatus
from src.replay.classifier import OutcomeClassifier


def test_outcome_classifier_business_outcome_branch() -> None:
    """Verify classifier routes 'No member found' to BUSINESS_OUTCOME instead of error."""
    checkpoint = StepCheckpoint(
        branches=[
            CheckpointBranch(
                condition_type="text_visible",
                pattern="Warning: No member found",
                outcome_category="business_outcome",
                outcome_code="MEMBER_NOT_FOUND",
                message="Member ID does not exist",
            ),
            CheckpointBranch(
                condition_type="text_visible",
                pattern="MEMBER RECORD:",
                outcome_category="continue",
            ),
        ]
    )

    page_content = "<html><body><div id='err'>Warning: No member found matching ID 9999</div></body></html>"
    res = OutcomeClassifier.evaluate_checkpoint_branches(checkpoint, page_content, "http://localhost:8000")

    assert res.should_continue is False
    assert res.outcome_category == "business_outcome"
    assert res.outcome_code == "MEMBER_NOT_FOUND"

    result = OutcomeClassifier.build_business_outcome("lookup_savings", 250, res.outcome_code, res.message)
    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome_code == "MEMBER_NOT_FOUND"


def test_outcome_classifier_continue_branch() -> None:
    """Verify classifier allows continuation when success condition met."""
    checkpoint = StepCheckpoint(
        branches=[
            CheckpointBranch(
                condition_type="text_visible",
                pattern="Warning: No member found",
                outcome_category="business_outcome",
                outcome_code="MEMBER_NOT_FOUND",
            ),
            CheckpointBranch(
                condition_type="text_visible",
                pattern="MEMBER RECORD:",
                outcome_category="continue",
            ),
        ]
    )

    page_content = "<html><body><h1>MEMBER RECORD: 1042 — Jane Doe</h1></body></html>"
    res = OutcomeClassifier.evaluate_checkpoint_branches(checkpoint, page_content, "http://localhost:8000")

    assert res.should_continue is True
    assert res.outcome_category == "continue"
