"""State Delta and Diff Computation Engine for Human-in-the-Loop Interventions.

Synthesizes pre-intervention and post-intervention state snapshots into structured,
auditable diffs with zero PII leakage.
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Union
from bs4 import BeautifulSoup
from src.schemas.hitl import HumanInterventionRecord, StateDelta, RecordedAction
from src.guardrails.pii_redactor import PIIRedactor


class StateDiffEngine:
    """Computes semantic deltas between pre- and post-intervention surface states."""

    @classmethod
    def compute_state_delta(
        cls,
        pre_url: str,
        post_url: str,
        pre_dom: str,
        post_dom: str,
        recorded_actions: List[RecordedAction],
    ) -> StateDelta:
        """Analyze DOM changes and URL navigation between pre- and post-handoff states."""
        raw_pre = (pre_url or "").strip()
        raw_post = (post_url or "").strip()
        safe_pre_url = PIIRedactor.redact_text(raw_pre)
        safe_post_url = PIIRedactor.redact_text(raw_post)
        url_changed = raw_pre != raw_post
        form_fields_changed: Dict[str, str] = {}

        # Extract form field modifications from recorded operator actions
        for act in recorded_actions:
            if act.action_type in ("change", "input", "type"):
                if isinstance(act.target, dict):
                    raw_field_name = act.target.get("name") or act.target.get("id") or act.target.get("selector") or "unknown_field"
                elif isinstance(act.target, str):
                    raw_field_name = act.target
                else:
                    raw_field_name = "unknown_field"
                field_name = PIIRedactor.redact_text(str(raw_field_name))
                val = act.value or ""
                # Ensure no sensitive tokens slip into diff summary (INV-27)
                safe_val = "[REDACTED_SENSITIVE_INPUT]" if act.is_masked else PIIRedactor.redact_text(val)
                form_fields_changed[field_name] = safe_val

        # Fast path if DOM didn't mutate (INV-06)
        if pre_dom == post_dom:
            elements_added_count = 0
            elements_removed_count = 0
            new_texts = []
            removed_texts = []
        else:
            # Structural difference analysis via BeautifulSoup with deterministic sorting
            try:
                pre_soup = BeautifulSoup((pre_dom or "")[:500000], "html.parser")
                post_soup = BeautifulSoup((post_dom or "")[:500000], "html.parser")

                for tag in pre_soup(["script", "style", "noscript", "template"]):
                    tag.decompose()
                for tag in post_soup(["script", "style", "noscript", "template"]):
                    tag.decompose()

                pre_texts = set(t.strip() for t in pre_soup.stripped_strings if len(t.strip()) >= 2)
                post_texts = set(t.strip() for t in post_soup.stripped_strings if len(t.strip()) >= 2)

                added_set = post_texts - pre_texts
                removed_set = pre_texts - post_texts
                elements_added_count = len(added_set)
                elements_removed_count = len(removed_set)

                new_texts = [PIIRedactor.redact_text(t) for t in sorted(added_set)][:10]
                removed_texts = [PIIRedactor.redact_text(t) for t in sorted(removed_set)][:10]
            except Exception:
                elements_added_count = 0
                elements_removed_count = 0
                new_texts = []
                removed_texts = []

        summary_parts = []
        if url_changed:
            summary_parts.append(f"URL changed from '{safe_pre_url}' to '{safe_post_url}'")
        if form_fields_changed:
            summary_parts.append(f"{len(form_fields_changed)} form fields modified: {sorted(form_fields_changed.keys())}")
        if new_texts:
            summary_parts.append(f"New visible text appeared: {new_texts[:3]}")
        if removed_texts:
            summary_parts.append(f"Visible text removed: {removed_texts[:3]}")

        summary = "; ".join(summary_parts) if summary_parts else "Operator performed in-situ inspection with no state mutation."

        return StateDelta(
            url_changed=url_changed,
            pre_url=safe_pre_url,
            post_url=safe_post_url,
            elements_added_count=elements_added_count,
            elements_removed_count=elements_removed_count,
            form_fields_changed=form_fields_changed,
            summary=summary,
        )

    @classmethod
    def export_intervention_evidence(
        cls,
        record: HumanInterventionRecord,
        output_path: Union[Path, str],
    ) -> Path:
        """Export serialized intervention record to evidence/human_intervention.json with zero PII egress (INV-28)."""
        target_path = Path(output_path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Redact any residual PII before writing to disk
        data = record.model_dump(mode="json")
        clean_data = PIIRedactor.redact_object(data)

        temp_path = target_path.parent / f"{target_path.name}.tmp.{os.getpid()}"
        try:
            temp_path.write_text(json.dumps(clean_data, indent=2), encoding="utf-8")
            temp_path.replace(target_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            target_path.write_text(json.dumps(clean_data, indent=2), encoding="utf-8")
        return target_path

