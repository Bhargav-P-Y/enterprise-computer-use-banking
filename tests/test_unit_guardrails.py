"""Level 1 Unit Tests: Safety Citadel (Allowlist, Risk Governor, PII Redactor)."""

import pytest
from src.schemas.artifact import ActionType, RiskLevel
from src.guardrails.allowlist import RouteAllowlist, SecurityPolicyViolation
from src.guardrails.risk_governor import RiskGovernor
from src.guardrails.pii_redactor import PIIRedactor


def test_route_allowlist_enforcement() -> None:
    """Verify allowlist permits configured local domains and blocks external malicious URLs."""
    allowlist = RouteAllowlist(["localhost:8000", "127.0.0.1:8000"])

    assert allowlist.is_url_allowed("http://localhost:8000/servicing/lookup") is True
    assert allowlist.is_url_allowed("http://127.0.0.1:8000/servicing/member/1042") is True
    assert allowlist.is_url_allowed("/servicing/transfer") is True
    assert allowlist.is_url_allowed("about:blank") is True

    # Blocked external domains
    assert allowlist.is_url_allowed("https://evil-phishing-bank.com/steal") is False
    assert allowlist.is_url_allowed("http://google.com") is False
    assert allowlist.is_url_allowed("http://localhost:9999/other") is False

    with pytest.raises(SecurityPolicyViolation):
        allowlist.assert_url_allowed("https://attacker.com/leak")


def test_risk_governor_classification() -> None:
    """Verify action risk governor tags read vs risky operations appropriately."""
    # Reads
    assert RiskGovernor.classify_action(ActionType.NAVIGATE) == RiskLevel.SAFE_READ
    assert RiskGovernor.classify_action(ActionType.EXTRACT_DATA) == RiskLevel.SAFE_READ
    assert (
        RiskGovernor.classify_action(ActionType.CLICK, target_text="Search", target_name="Lookup Member")
        == RiskLevel.SAFE_READ
    )

    # Safe typing
    assert (
        RiskGovernor.classify_action(ActionType.TYPE_TEXT, target_name="Member ID", value="1042")
        == RiskLevel.SAFE_IDEMPOTENT_WRITE
    )

    # Risky financial operations
    assert (
        RiskGovernor.classify_action(ActionType.CLICK, target_text="Submit Transfer")
        == RiskLevel.RISKY_IRREVERSIBLE
    )
    assert (
        RiskGovernor.classify_action(ActionType.CLICK, target_name="Authorize and Proceed")
        == RiskLevel.RISKY_IRREVERSIBLE
    )
    assert (
        RiskGovernor.classify_action(ActionType.CLICK, target_text="Confirm Wire Payment")
        == RiskLevel.RISKY_IRREVERSIBLE
    )

    # Confirmation gating
    assert RiskGovernor.requires_confirmation(RiskLevel.SAFE_READ) is False
    assert RiskGovernor.requires_confirmation(RiskLevel.RISKY_IRREVERSIBLE, strict_mode=True) is True
    assert RiskGovernor.requires_confirmation(RiskLevel.RISKY_IRREVERSIBLE, strict_mode=False) is False


def test_pii_redactor_patterns() -> None:
    """Verify in-memory PII scrubber replaces SSNs, cards, accounts, emails, and credentials."""
    # SSN
    text_ssn = "Customer SSN is 123-45-6789 on file."
    assert PIIRedactor.redact_text(text_ssn) == "Customer SSN is [REDACTED_SSN] on file."

    # Account numbers
    text_acc = "Transferred from SAV-1042-01 to CHK-2088-02."
    assert PIIRedactor.redact_text(text_acc) == "Transferred from [REDACTED_ACCT] to [REDACTED_ACCT]."

    # Email
    text_email = "Contact customer at jane.doe@example.com immediately."
    assert PIIRedactor.redact_text(text_email) == "Contact customer at [REDACTED_EMAIL] immediately."

    # Object redaction
    payload = {
        "member_id": "1042",
        "ssn": "123-45-6789",
        "password": "SuperSecretPassword123!",
        "override_code": "MGR-9941",
        "nested": {
            "card_num": "4111 2222 3333 4444",
            "safe_field": "Normal text",
        },
    }
    clean = PIIRedactor.redact_object(payload)
    assert clean["ssn"] == "[REDACTED_CREDENTIAL]"
    assert clean["password"] == "[REDACTED_CREDENTIAL]"
    assert clean["override_code"] == "[REDACTED_CREDENTIAL]"
    assert clean["nested"]["card_num"] == "[REDACTED_CARD]"
    assert clean["nested"]["safe_field"] == "Normal text"
    assert clean["member_id"] == "1042"
