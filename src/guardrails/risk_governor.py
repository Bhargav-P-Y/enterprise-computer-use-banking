"""Action risk governor distinguishing safe/reversible from risky/irreversible actions."""

import re
from typing import Optional, Union
from src.schemas.artifact import ActionType, RiskLevel

# Fast substring pre-check set before invoking regex engine (INV-06)
_RISKY_TOKENS = {
    "confirm", "submit", "transfer", "delete", "remove",
    "close", "modify", "override", "authorize", "pay", "wire",
    "disburse", "approve", "execute", "post", "refund", "void", "payout",
    "withdraw", "liquidate", "revoke", "terminate", "payment", "disbursement",
}

RISKY_KEYWORDS = re.compile(
    r"\b(confirm|submit|transfer|transfers|delete|remove|close|modify|override|authorize|pay|payment|payments|wire|disburse|disbursement|approve|execute|post|refund|void|payout|withdraw|withdrawal|withdrawals|liquidate|revoke|terminate)\b",
    re.IGNORECASE,
)


class RiskGovernor:
    """Classifies and gates actions based on potential financial and operational risk."""

    @classmethod
    def classify_action(
        cls,
        action_type: Union[ActionType, str],
        target_text: Optional[str] = None,
        target_name: Optional[str] = None,
        value: Optional[str] = None,
    ) -> RiskLevel:
        """Classify an action into safe read, safe write, or risky irreversible."""
        # Coerce string action_type to Enum if needed
        if isinstance(action_type, str):
            try:
                action_type = ActionType(action_type.lower())
            except ValueError:
                action_type = ActionType.CLICK

        # Read-only actions are always safe
        if action_type in (ActionType.NAVIGATE, ActionType.EXTRACT_DATA, ActionType.WAIT_FOR_STATE):
            return RiskLevel.SAFE_READ

        # Bounded string assembly to prevent ReDoS / memory exhaustion on hostile inputs (INV-06)
        text_parts = []
        for s in (target_text, target_name):
            if s:
                text_parts.append(str(s).strip()[:512])
        # Restrict value inspection to non-text entry to prevent routine memo typing false-positives
        if action_type != ActionType.TYPE_TEXT and value:
            text_parts.append(str(value).strip()[:512])
        text_to_check = " ".join(text_parts).strip()

        if text_to_check:
            lower_text = text_to_check.lower()
            # Fast-path algorithmic pre-filter before regex traversal (INV-06)
            if any(token in lower_text for token in _RISKY_TOKENS):
                if RISKY_KEYWORDS.search(text_to_check):
                    return RiskLevel.RISKY_IRREVERSIBLE

        if action_type in (ActionType.TYPE_TEXT, ActionType.SELECT_OPTION):
            return RiskLevel.SAFE_IDEMPOTENT_WRITE

        return RiskLevel.SAFE_READ

    @staticmethod
    def requires_confirmation(risk_level: RiskLevel, strict_mode: bool = True) -> bool:
        """Determine whether human confirmation or policy gating is required."""
        if not strict_mode:
            return False
        return risk_level == RiskLevel.RISKY_IRREVERSIBLE

