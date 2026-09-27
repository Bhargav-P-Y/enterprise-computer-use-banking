"""Unit tests for HITL schemas and StateDiffEngine."""

from pathlib import Path
from src.schemas.hitl import HumanInterventionRecord, RecordedAction, StateDelta
from src.hitl.diff_engine import StateDiffEngine


def test_hitl_schema_validation() -> None:
    """Verify HumanInterventionRecord valid construction."""
    record = HumanInterventionRecord(
        intervention_id="hitl_test_001",
        capability_id="transfer_funds",
        trigger_context={"reason": "Step timed out on security modal"},
        pre_state={"url": "http://127.0.0.1:8123/servicing/transfer"},
        recorded_actions=[
            RecordedAction(
                sequence=1,
                action_type="click",
                target={"id": "btnOverride", "tag": "button"},
                timestamp_ms=1000,
            )
        ],
        post_state={"url": "http://127.0.0.1:8123/servicing/transfer"},
        state_delta=StateDelta(
            url_changed=False,
            pre_url="http://127.0.0.1:8123/servicing/transfer",
            post_url="http://127.0.0.1:8123/servicing/transfer",
            summary="Operator clicked override",
        ),
    )
    assert record.intervention_id == "hitl_test_001"
    assert len(record.recorded_actions) == 1
    assert record.recorded_actions[0].action_type == "click"


def test_diff_engine_computation_and_masking(tmp_path: Path) -> None:
    """Verify StateDiffEngine masks sensitive fields and identifies state changes."""
    pre_dom = "<html><body><h1>Transfer Console</h1></body></html>"
    post_dom = "<html><body><h1>Transfer Approved</h1><div>Receipt: #8821</div></body></html>"

    actions = [
        RecordedAction(
            sequence=1,
            action_type="change",
            target={"name": "account_ssn", "id": "txtSSN"},
            value="123-45-6789",
            is_masked=True,
            timestamp_ms=2000,
        ),
        RecordedAction(
            sequence=2,
            action_type="change",
            target={"name": "transfer_memo"},
            value="Emergency rent payment",
            is_masked=False,
            timestamp_ms=2500,
        ),
    ]

    delta = StateDiffEngine.compute_state_delta(
        pre_url="http://bank/transfer",
        post_url="http://bank/receipt",
        pre_dom=pre_dom,
        post_dom=post_dom,
        recorded_actions=actions,
    )

    assert delta.url_changed is True
    assert "txtSSN" in delta.form_fields_changed or "account_ssn" in delta.form_fields_changed
    # Sensitive field must be masked
    ssn_val = delta.form_fields_changed.get("account_ssn", "")
    assert "[REDACTED_SENSITIVE_INPUT]" in ssn_val

    # Export check
    record = HumanInterventionRecord(
        intervention_id="hitl_test_export",
        capability_id="transfer_funds",
        state_delta=delta,
    )
    out_file = tmp_path / "human_intervention.json"
    StateDiffEngine.export_intervention_evidence(record, out_file)
    assert out_file.exists()

    content = out_file.read_text(encoding="utf-8")
    assert "123-45-6789" not in content  # Strict PII firewall verification
