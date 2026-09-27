import html
import re
from typing import Any, Dict, Optional
from pydantic import BaseModel
from src.schemas.artifact import StepCheckpoint
from src.schemas.execution import ReplayResult, ReplayStatus
from src.guardrails.pii_redactor import PIIRedactor

_SPECIAL_REGEX_CHARS = set(r".^$*+?{}[]\|()")


class BranchEvaluationResult(BaseModel):
    """Structured result of evaluating checkpoint branching rules."""
    should_continue: bool
    outcome_category: str = "continue"
    outcome_code: Optional[str] = None
    message: Optional[str] = None


class OutcomeClassifier:
    """Evaluates page states and checkpoints against the Tri-Partite Error Taxonomy."""

    @classmethod
    def evaluate_checkpoint_branches(
        cls,
        checkpoint: Optional[StepCheckpoint],
        page_text: str,
        current_url: str,
    ) -> BranchEvaluationResult:
        """
        Evaluate checkpoint branching rules against current DOM text and URL.
        Features fast literal string pre-filtering and resilient regex error trapping (INV-06, INV-20).
        """
        if not checkpoint or not checkpoint.branches:
            return BranchEvaluationResult(should_continue=True)

        page_text = page_text or ""
        current_url = current_url or ""
        # Bound raw scan to 500k chars to remove bulky ViewState before taking bounded 50k slice (INV-06, INV-08, INV-24)
        raw_slice = page_text[:500000]
        # Strip script tags, styles, and bulky ViewState strings to extract semantic visible text representation
        cleaned_text = re.sub(r"(?is)<(?:script|style)[^>]*>.*?</(?:script|style)>", "", raw_slice)
        cleaned_text = re.sub(r'(?i)<input[^>]*name=["\']__VIEWSTATE[^>]*>', "", cleaned_text)
        cleaned_text = re.sub(r"(?i)__VIEWSTATE[a-zA-Z0-9+/=]*", "", cleaned_text)
        bounded_text = html.unescape(cleaned_text[:50000])
        # Pre-compute unified lowercase text once outside branch loop to avoid O(N*M) allocations
        bounded_clean_lower = bounded_text.lower()
        url_clean_lower = current_url.lower()

        for branch in checkpoint.branches:
            if not branch.pattern or not branch.pattern.strip():
                continue

            pattern_clean = branch.pattern.strip()[:256]
            condition_met = False

            if branch.condition_type == "text_visible":
                # Fast pre-filter against unified clean visible text (INV-06)
                is_pure_literal = not any(c in _SPECIAL_REGEX_CHARS for c in pattern_clean)
                if is_pure_literal:
                    if pattern_clean.lower() in bounded_clean_lower:
                        condition_met = True
                else:
                    # Robust bounded regex fallback with syntax error trap
                    try:
                        if re.search(pattern_clean, bounded_text, re.IGNORECASE):
                            condition_met = True
                    except (re.error, Exception):
                        condition_met = False

            elif branch.condition_type == "url_matches":
                is_pure_literal = not any(c in _SPECIAL_REGEX_CHARS for c in pattern_clean)
                pattern_lower = pattern_clean.lower()
                if is_pure_literal:
                    if pattern_lower in url_clean_lower:
                        condition_met = True
                else:
                    if pattern_lower in url_clean_lower:
                        condition_met = True
                    else:
                        try:
                            if re.search(pattern_clean, current_url, re.IGNORECASE):
                                condition_met = True
                        except (re.error, Exception):
                            condition_met = False
            else:
                # Safely ignore unrecognized condition types without crashing (INV-20)
                continue

            if condition_met:
                clean_msg = PIIRedactor.redact_text(branch.message) if branch.message else None
                if branch.outcome_category == "business_outcome":
                    return BranchEvaluationResult(
                        should_continue=False,
                        outcome_category="business_outcome",
                        outcome_code=branch.outcome_code,
                        message=clean_msg,
                    )
                elif branch.outcome_category == "hard_failure":
                    return BranchEvaluationResult(
                        should_continue=False,
                        outcome_category="hard_failure",
                        outcome_code=branch.outcome_code,
                        message=clean_msg,
                    )
                elif branch.outcome_category == "continue":
                    return BranchEvaluationResult(should_continue=True)

        return BranchEvaluationResult(should_continue=True)

    @classmethod
    def build_business_outcome(
        cls,
        capability_id: str,
        duration_ms: int,
        code: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> ReplayResult:
        """Construct a structured ReplayResult for expected business states (NEVER a crash)."""
        clean_msg = PIIRedactor.redact_text(message)
        clean_data = PIIRedactor.redact_object(data or {})
        return ReplayResult(
            status=ReplayStatus.BUSINESS_OUTCOME,
            capability_id=capability_id,
            duration_ms=duration_ms,
            business_outcome_code=code,
            message=clean_msg,
            data=clean_data,
        )

    @classmethod
    def build_hard_failure(
        cls,
        capability_id: str,
        duration_ms: int,
        failed_step_id: str,
        expected: str,
        observed: str,
        evidence_paths: Optional[Dict[str, str]] = None,
    ) -> ReplayResult:
        """Construct a structured ReplayResult for hard unrecoverable system failures with zero PII egress (INV-28)."""
        clean_expected = PIIRedactor.redact_text(expected)
        clean_observed = PIIRedactor.redact_text(observed)
        clean_step_id = PIIRedactor.redact_text(failed_step_id)
        clean_evidence = PIIRedactor.redact_object(evidence_paths or {})
        return ReplayResult(
            status=ReplayStatus.HARD_FAILURE,
            capability_id=capability_id,
            duration_ms=duration_ms,
            message=f"Hard failure at step {clean_step_id}: expected {clean_expected}, observed {clean_observed}",
            failure_context={
                "failed_step_id": clean_step_id,
                "expected": clean_expected,
                "observed": clean_observed,
                "evidence_paths": clean_evidence,
            },
        )
