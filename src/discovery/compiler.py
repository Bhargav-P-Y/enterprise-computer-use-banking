"""Capability Artifact Compiler.

Transforms verified exploratory discovery traces into typed, versioned,
deterministic CapabilityArtifact specifications (Pydantic v2) with multi-tier
locators, parameterization, and branching checkpoints.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from src.schemas.artifact import (
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
    ActionType,
    StepCheckpoint,
    StepExtraction,
)
from src.guardrails.risk_governor import RiskGovernor
from src.guardrails.pii_redactor import PIIRedactor

VALID_ACTION_VALUES = {a.value for a in ActionType}


class CapabilityCompiler:
    """Compiles exploratory traces into production-grade CapabilityArtifacts."""

    @classmethod
    def compile_trace(
        cls,
        capability_id: str,
        description: str,
        target_app: str,
        base_url_pattern: str,
        raw_steps: List[Dict[str, Any]],
        known_inputs: Optional[Dict[str, str]] = None,
        extracted_fields: Optional[List[str]] = None,
    ) -> CapabilityArtifact:
        """Compile a list of executed discovery steps into a serialized CapabilityArtifact."""
        known_inputs = known_inputs or {}
        extracted_fields = extracted_fields or []

        # 1. Prune redundant exploratory actions
        pruned_steps = cls._prune_trace(raw_steps)

        # 2. Synthesize Steps with Parameterization & Multi-Tier Cascades
        compiled_steps: List[CapabilityStep] = []
        input_params: Dict[str, ParameterProperty] = {}
        output_params: Dict[str, ParameterProperty] = {
            fld: ParameterProperty(type="string", description=f"Extracted field: {fld}")
            for fld in extracted_fields
        }

        # O(K log K) sort performed once outside the loop (INV-06)
        sorted_inputs = sorted(known_inputs.items(), key=lambda item: len(str(item[1] or "")), reverse=True)

        for idx, step in enumerate(pruned_steps):
            step_id = f"step_{idx + 1}_{step.get('action', 'action')}"
            action_str = step.get("action", "").lower()
            raw_val = step.get("value")

            # Check parameterization with boundary-safe matching, longest literal first (INV-16)
            value_binding = raw_val
            if raw_val is not None and str(raw_val).strip() != "":
                matched_param = False
                current_val = str(raw_val)
                for param_name, literal_val in sorted_inputs:
                    if not literal_val:
                        continue
                    lit_str = str(literal_val)
                    pattern = rf"(?<!\w){re.escape(lit_str)}(?!\w)" if re.match(r"^\w+$", lit_str) else re.escape(lit_str)
                    if re.search(pattern, current_val):
                        current_val = re.sub(pattern, f"$input.{param_name}", current_val)
                        input_params[param_name] = ParameterProperty(
                            type="string",
                            description=f"Input parameter for {param_name}",
                        )
                        matched_param = True
                if matched_param:
                    # Protect $input. placeholders while scrubbing any residual PII (INV-27, INV-28)
                    tokens: Dict[str, str] = {}
                    def _stash(m: re.Match) -> str:
                        tok = f"__PARAM_TOKEN_{len(tokens)}__"
                        tokens[tok] = m.group(0)
                        return tok
                    stashed = re.sub(r"\$input\.[A-Za-z0-9_\-]+", _stash, current_val)
                    sanitized = PIIRedactor.redact_text(stashed)
                    for tok, original in tokens.items():
                        sanitized = sanitized.replace(tok, original)
                    value_binding = sanitized
                else:
                    # Sanitize any literal values against PII leakage (INV-27)
                    value_binding = PIIRedactor.redact_text(current_val)

            # Synthesize Multi-Tier Locator Cascade across all interactive DOM action types
            locator = None
            if action_str in ("click", "type_text", "select_option", "check_box", "hover", "press_key"):
                locator = cls._synthesize_locator(step)

            # Synthesize Checkpoints
            checkpoint = None
            if step.get("checkpoint_rule"):
                rule = step["checkpoint_rule"]
                checkpoint = StepCheckpoint(
                    branches=[
                        CheckpointBranch(
                            condition_type="text_visible",
                            pattern=rule.get("warning_pattern", "Warning"),
                            outcome_category="business_outcome",
                            outcome_code=rule.get("outcome_code", "BUSINESS_OUTCOME"),
                            message=f"Business exception triggered: {rule.get('outcome_code')}",
                        ),
                        CheckpointBranch(
                            condition_type="text_visible",
                            pattern=step.get("expected_feedback", "Success"),
                            outcome_category="continue",
                        ),
                    ]
                )

            # Synthesize Extractions if action is extract_data
            extractions: List[StepExtraction] = []
            if action_str == "extract_data":
                field_name = step.get("extraction_field", "extracted_value")
                transform = step.get("transform", "trim_string")
                ext_locator = cls._synthesize_locator(step)
                extractions.append(
                    StepExtraction(
                        output_field=field_name,
                        locator=ext_locator,
                        transform=transform,
                    )
                )
                output_params[field_name] = ParameterProperty(
                    type="number" if transform == "parse_currency" else "string",
                    description=f"Extracted field {field_name}",
                )

            cap_action = ActionType(action_str) if action_str in VALID_ACTION_VALUES else ActionType.CLICK

            target_frame = step.get("target_frame")
            raw_desc = step.get("target_description") or f"Execute {action_str}"
            safe_step_desc = PIIRedactor.redact_text(raw_desc)

            compiled_steps.append(
                CapabilityStep(
                    step_id=step_id,
                    description=safe_step_desc,
                    action=cap_action,
                    locator=locator,
                    value_binding=value_binding,
                    target_frame=target_frame,
                    checkpoint=checkpoint,
                    extractions=extractions,
                )
            )

        # 3. Assess Risk Level
        risk_levels = [
            RiskGovernor.classify_action(
                action_type=s.action,
                target_text=s.description,
                value=s.value_binding,
            )
            for s in compiled_steps
        ]
        if RiskLevel.RISKY_IRREVERSIBLE in risk_levels:
            risk_level = RiskLevel.RISKY_IRREVERSIBLE
        elif RiskLevel.SAFE_IDEMPOTENT_WRITE in risk_levels:
            risk_level = RiskLevel.SAFE_IDEMPOTENT_WRITE
        else:
            risk_level = RiskLevel.SAFE_READ

        safe_metadata_desc = PIIRedactor.redact_text(description)

        return CapabilityArtifact(
            schema_version="1.0.0",
            metadata=CapabilityMetadata(
                capability_id=capability_id,
                description=safe_metadata_desc,
                target_app=target_app,
                base_url_pattern=base_url_pattern,
                risk_level=risk_level,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
            ),
            interface=CapabilityInterface(
                inputs=input_params,
                required_inputs=list(input_params.keys()),
                outputs=output_params,
                required_outputs=list(output_params.keys()),
            ),
            steps=compiled_steps,
        )

    @classmethod
    def _prune_trace(cls, raw_steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter out redundant steps, duplicate extractions, no-ops, exploratory cycles/backtracks, and finish actions (INV-15)."""
        pruned = []
        # Pass 1: Filter out cancelled/failed/finish/noop sentinel actions
        for s in raw_steps:
            action = s.get("action", "").lower()
            if action in ("finish", "escalate_hitl", "noop"):
                continue
            if s.get("status") in ("BLOCKED_PENDING_CONFIRMATION", "FAILED", "CANCELLED"):
                continue
            pruned.append(s)

        # Pass 2: True DAG cycle & backtrack pruning (INV-15)
        # If an exploratory path navigated back to a previous URL or state, purge intervening dead-end steps
        dag_steps: List[Dict[str, Any]] = []
        url_indices: Dict[str, int] = {}
        for s in pruned:
            curr_url = s.get("url") or (s.get("value") if s.get("action") == "navigate" else None)
            if curr_url and curr_url in url_indices and s.get("action") == "navigate":
                # Cycle detected: agent returned to a previously visited URL. Prune dead-end loop.
                prev_idx = url_indices[curr_url]
                dag_steps = dag_steps[: prev_idx + 1]
                url_indices = {
                    (step.get("url") or step.get("value")): i
                    for i, step in enumerate(dag_steps)
                    if step.get("action") == "navigate"
                }
            else:
                if curr_url and s.get("action") == "navigate":
                    url_indices[curr_url] = len(dag_steps)
                dag_steps.append(s)

        # Pass 3: Extraction deduplication after cycle pruning (preserving canonical extraction on final DAG)
        final_steps: List[Dict[str, Any]] = []
        seen_extractions = set()
        for s in reversed(dag_steps):
            action = s.get("action", "").lower()
            if action == "extract_data":
                field = s.get("extraction_field", "default")
                if field in seen_extractions:
                    continue
                seen_extractions.add(field)
            final_steps.append(s)
        final_steps.reverse()

        return final_steps

    @classmethod
    def _synthesize_locator(cls, step: Dict[str, Any]) -> LocatorCascade:
        """Construct a guaranteed 4-tier resilient locator cascade from discovery observation (INV-10, INV-27, INV-28)."""
        raw_desc = step.get("target_description", "")
        desc = PIIRedactor.redact_text(raw_desc)
        hint = step.get("selector_hint")
        som = step.get("som_label")

        tiers: List[LocatorTier] = []

        # Tier 1: Semantic Role & Accessible Name (Mandatory top tier - INV-10)
        label_text = step.get("label_text")
        if label_text:
            label_text = PIIRedactor.redact_text(label_text)
        else:
            cleaned = re.sub(r"(input|box|text|field|button|submit)", "", desc, flags=re.IGNORECASE).strip()
            label_text = cleaned if cleaned else desc.strip()

        if "input" in desc.lower() or "box" in desc.lower():
            label_candidate = re.sub(r"(input|box|text|field)", "", desc, flags=re.IGNORECASE).strip() or desc.strip() or "textbox"
            tiers.append(LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role="textbox", name=PIIRedactor.redact_text(label_candidate)))
        elif "button" in desc.lower() or "submit" in desc.lower():
            label_candidate = re.sub(r"(button|btn|submit)", "", desc, flags=re.IGNORECASE).strip() or desc.strip() or "button"
            tiers.append(LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role="button", name=PIIRedactor.redact_text(label_candidate)))
        else:
            role = "textbox" if step.get("action") == "type_text" else "button"
            name = label_text or desc.strip()[:30] or "control"
            tiers.append(LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role=role, name=PIIRedactor.redact_text(name)))

        # Tier 2: Spatial Label Proximity (Mandatory 4-tier completeness - INV-10)
        prox_label = label_text or desc.strip()[:30] or "label"
        tiers.append(LocatorTier(strategy=LocatorStrategyType.LABEL_PROXIMITY, label_text=PIIRedactor.redact_text(prox_label), direction="right"))

        # Tier 3: Structural XPath (Standards-compliant XPath 1.0, injection-safe - INV-11)
        clean_label = re.sub(r'[\'\"\\\[\]]', '', PIIRedactor.redact_text(label_text or "")).strip()
        if step.get("xpath"):
            safe_xpath = PIIRedactor.redact_text(step["xpath"])
            # Normalize parenthesized unions like //(a|b)[...] to //a[...] | //b[...]
            m = re.match(r"^//\(([^)]+)\)(.*)$", safe_xpath)
            if m:
                tags = [t.strip() for t in m.group(1).split("|")]
                tail = m.group(2)
                safe_xpath = " | ".join(f"//{t}{tail}" for t in tags)
            tiers.append(LocatorTier(strategy=LocatorStrategyType.STRUCTURAL_XPATH, xpath=safe_xpath))
        elif hint and ("//" in hint or "/" in hint):
            norm_hint = re.sub(r"text\(\)\s*=\s*'([^']+)'", r"contains(., '\1')", hint)
            safe_xpath = PIIRedactor.redact_text(norm_hint)
            tiers.append(LocatorTier(strategy=LocatorStrategyType.STRUCTURAL_XPATH, xpath=safe_xpath))
        else:
            tag = "input" if "input" in desc.lower() else "button" if "button" in desc.lower() else "*"
            if clean_label:
                tiers.append(LocatorTier(strategy=LocatorStrategyType.STRUCTURAL_XPATH, xpath=f"//{tag}[@value='{clean_label}'] | //button[contains(., '{clean_label}')]"))
            else:
                tiers.append(LocatorTier(strategy=LocatorStrategyType.STRUCTURAL_XPATH, xpath=f"//{tag}"))

        # Tier 4: Coordinate Ratio fallback (INV-10)
        coords = step.get("coordinates_ratio") or [0.5, 0.5]
        tiers.append(LocatorTier(strategy=LocatorStrategyType.COORDINATE_RATIO, coords_ratio=coords))

        return LocatorCascade(
            tiers=tiers,
            robustness_rationale=PIIRedactor.redact_text(f"Synthesized 4-tier cascade for '{desc}'"),
        )
