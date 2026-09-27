"""Autonomous Multimodal Discovery Agent.

Orchestrates the observe-decide-act loop on a live browser surface using Gemini 3.8 Flash,
compiling successful exploratory trajectories into deterministic CapabilityArtifacts.
"""

import asyncio
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.surface.driver import SurfaceDriver
from src.discovery.gemini_client import DiscoveryGeminiClient
from src.discovery.prompts import DISCOVERY_SYSTEM_INSTRUCTION, build_discovery_user_prompt
from src.discovery.compiler import CapabilityCompiler
from src.schemas.artifact import CapabilityArtifact, RiskLevel
from src.guardrails.pii_redactor import PIIRedactor
from src.guardrails.risk_governor import RiskGovernor
from src.hitl.handoff import InSituHandoffManager


class DiscoveryAgent:
    """Multimodal computer-use agent exploring back-office web surfaces."""

    def __init__(
        self,
        driver: SurfaceDriver,
        gemini_client: Optional[DiscoveryGeminiClient] = None,
        evidence_dir: Optional[Path] = None,
        handoff_manager: Optional[InSituHandoffManager] = None,
    ) -> None:
        self.driver = driver
        self.gemini_client = gemini_client or DiscoveryGeminiClient()
        self.evidence_dir = evidence_dir or Path("evidence")
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.handoff_manager = handoff_manager or InSituHandoffManager(driver=self.driver, evidence_dir=self.evidence_dir)
        self.execution_log: List[Dict[str, Any]] = []

    async def discover_capability(
        self,
        capability_id: str,
        goal: str,
        start_url: str,
        target_app: str = "CoreBankingConsole",
        known_inputs: Optional[Dict[str, str]] = None,
        max_steps: int = 8,
    ) -> CapabilityArtifact:
        """Execute autonomous discovery loop and compile resulting capability artifact (INV-01, INV-13)."""
        known_inputs = known_inputs or {}
        raw_steps: List[Dict[str, Any]] = []

        try:
            # Initial navigation to target start URL (INV-03, INV-25)
            # SurfaceDriver.navigate encapsulates allowlist verification adhering to Law of Demeter
            await self.driver.navigate(start_url)
            raw_steps.append({
                "step_num": 1,
                "action": "navigate",
                "target_description": "Initial entrypoint navigation",
                "value": start_url,
            })
            self._log_event(f"Step 1: Navigated to {PIIRedactor.redact_text(start_url)}")

            for step_idx in range(2, max_steps + 1):
                # 1. Observe: Capture visual screenshot & interactive landmarks via SurfaceDriver (INV-03 Demeter)
                screenshot_bytes = await self.driver.capture_screenshot(quality=70, timeout=5000)
                current_url = self.driver.current_url
                target_frame_context = raw_steps[-1].get("target_frame") if raw_steps else None
                landmarks = await self._extract_visible_landmarks(target_frame=target_frame_context)

                # 2. Decide: Multimodal call to Gemini with sanitized history (INV-27, INV-28)
                clean_history = PIIRedactor.redact_object(raw_steps)
                user_prompt = build_discovery_user_prompt(
                    goal=goal,
                    step_history=clean_history,
                    current_url=PIIRedactor.redact_text(current_url),
                    available_elements_summary=landmarks,
                )

                # Non-blocking VLM inference with bounded execution budget (INV-07)
                decision = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.gemini_client.generate_decision,
                        system_instruction=DISCOVERY_SYSTEM_INSTRUCTION,
                        user_prompt=user_prompt,
                        screenshot_png_bytes=screenshot_bytes,
                    ),
                    timeout=35.0,
                )

                action = decision.get("action", "finish").lower()
                thought = decision.get("thought", "")
                target_desc = decision.get("target_description", "")
                val = decision.get("value")
                hint = decision.get("selector_hint")
                target_frame = decision.get("target_frame")

                self._log_event(f"Step {step_idx}: Thought='{thought}' | Action='{action}' | Target='{target_desc}' | Value='{val}'")

                # Dynamic checkpoint rule extraction without hardcoding (INV-17)
                checkpoint_rule = decision.get("checkpoint_rule")
                if not checkpoint_rule and decision.get("warning_pattern"):
                    checkpoint_rule = {
                        "warning_pattern": decision.get("warning_pattern"),
                        "outcome_code": decision.get("outcome_code", "BUSINESS_OUTCOME"),
                    }

                if action == "finish":
                    raw_steps.append({"step_num": step_idx, "action": "finish", "details": decision})
                    break

                # 3. Act: Execute action on live Chromium page via SurfaceDriver (INV-02, INV-03)
                act_record = {
                    "step_num": step_idx,
                    "action": action,
                    "target_description": target_desc,
                    "value": val,
                    "selector_hint": hint,
                    "target_frame": target_frame,
                    "checkpoint_rule": checkpoint_rule,
                    "expected_feedback": decision.get("expected_feedback"),
                    "extraction_field": decision.get("extraction_field"),
                    "transform": decision.get("transform"),
                }

                executed = await self._execute_action(capability_id, action, act_record)
                act_record["status"] = "SUCCESS" if executed else "FAILED"
                raw_steps.append(act_record)

                if not executed:
                    self._log_event(f"Warning: Step {step_idx} action execution experienced issues; continuing...")

                if action == "extract_data" and executed:
                    self._log_event(f"Step {step_idx}: Extracted data for '{act_record.get('extraction_field', 'target')}'; goal fulfilled.")
                    break

                # Bounded pause for DOM rendering (INV-07)
                await asyncio.sleep(0.5)

            # 4. Compile raw exploratory steps into Capability Artifact (INV-15, INV-16, INV-18)
            parsed_url = urllib.parse.urlsplit(start_url)
            clean_path = parsed_url.path.rstrip('/')
            if clean_path:
                path_prefix = clean_path.rsplit('/', 1)[0] if '/' in clean_path.lstrip('/') else clean_path
                base_url_pattern = f"{parsed_url.scheme}://{parsed_url.netloc}{path_prefix}/*"
            else:
                base_url_pattern = f"{parsed_url.scheme}://{parsed_url.netloc}/*"

            artifact = CapabilityCompiler.compile_trace(
                capability_id=capability_id,
                description=goal,
                target_app=target_app,
                base_url_pattern=base_url_pattern,
                raw_steps=raw_steps,
                known_inputs=known_inputs,
            )

            # 5. Export Evidence with Dual Partitioning (INV-22, INV-28)
            self._export_evidence(artifact, raw_steps)
            return artifact

        except Exception as unhandled_err:
            clean_err = PIIRedactor.redact_text(str(unhandled_err))[:120]
            self._log_event(f"Discovery session error: {type(unhandled_err).__name__}: {clean_err}")
            # Hard Failure Evidence Capture (INV-22)
            try:
                safe_cap_id = re.sub(r"[^a-zA-Z0-9_-]", "_", capability_id)
                err_screenshot = await self.driver.capture_screenshot(quality=50, timeout=2000)
                (self.evidence_dir / f"{safe_cap_id}_error_screenshot.jpg").write_bytes(err_screenshot)
                raw_dom = await self.driver.evaluate_script("() => document.documentElement.outerHTML")
                clean_dom = PIIRedactor.redact_text(raw_dom or "")
                (self.evidence_dir / f"{safe_cap_id}_error_dom.html").write_text(clean_dom, encoding="utf-8")
            except Exception:
                pass
            raise

    async def _execute_action(self, capability_id: str, action: str, act_record: Dict[str, Any]) -> bool:
        """Execute single action via SurfaceDriver with allowlist, risk gating, and HITL seam (INV-02, INV-03, INV-25, INV-26, INV-29)."""
        target_frame_name = act_record.get("target_frame")
        val = act_record.get("value")
        target_desc = act_record.get("target_description", "")
        hint = act_record.get("selector_hint")

        # Action Risk Governor verification & In-Situ HITL Seam (INV-26, INV-29)
        risk = RiskGovernor.classify_action(
            action_type=action,
            target_text=target_desc,
            value=val,
        )
        if risk == RiskLevel.RISKY_IRREVERSIBLE or action == "escalate_hitl":
            self._log_event(
                f"GATED: Action '{action}' on '{target_desc}' classified as RISKY_IRREVERSIBLE (INV-26, INV-29). Initiating In-Situ HITL handoff..."
            )
            act_record["requires_confirmation"] = True
            act_record["status"] = "BLOCKED_PENDING_CONFIRMATION"
            if self.handoff_manager:
                try:
                    record = await self.handoff_manager.initiate_handoff(
                        capability_id=capability_id,
                        trigger_reason=f"RiskGovernor gated {action} on {target_desc} as {risk.value}",
                        context=act_record,
                        timeout_seconds=60,
                    )
                    act_record["hitl_record_id"] = record.intervention_id
                    act_record["status"] = "CONFIRMED_VIA_HITL"
                    return True
                except Exception as hitl_err:
                    self._log_event(f"HITL takeover aborted or timed out: {PIIRedactor.redact_text(str(hitl_err))[:100]}")
            return False

        try:
            if action == "navigate" and val:
                # SSRF guard on all mid-discovery navigation via SurfaceDriver (INV-03, INV-25)
                await self.driver.navigate(val)
                return True

            elif action == "type_text":
                clean_label = re.sub(r"(input|box|text|field)", "", target_desc, flags=re.IGNORECASE).strip() or target_desc.strip()
                safe_label = re.sub(r'[\'\"\\\[\]]', '', clean_label)
                act_record["label_text"] = act_record.get("label_text") or clean_label or "Input Field"
                act_record["xpath"] = act_record.get("xpath") or f"//input[contains(@name, '{safe_label}') or contains(@id, '{safe_label}')] | //input[not(@type='hidden')][1]"

                # Delegated entirely to SurfaceDriver semantic primitive (INV-02, INV-03)
                executed = await self.driver.fill(
                    value=str(val or ""),
                    selector=hint,
                    target_frame=target_frame_name,
                    timeout_ms=3000,
                    label_fallback=act_record["label_text"],
                )
                return executed

            elif action == "click":
                clean_btn = re.sub(r"(button|btn|submit|click)", "", target_desc, flags=re.IGNORECASE).strip() or target_desc.strip()
                safe_btn = re.sub(r'[\'\"\\\[\]]', '', clean_btn)
                act_record["label_text"] = act_record.get("label_text") or clean_btn or "Submit"
                act_record["xpath"] = act_record.get("xpath") or f"//input[@value='{safe_btn}'] | //button[contains(., '{safe_btn}')] | //input[@type='submit']"

                # Delegated entirely to SurfaceDriver semantic primitive (INV-02, INV-03)
                executed = await self.driver.click(
                    selector=hint,
                    target_frame=target_frame_name,
                    timeout_ms=3000,
                    text_fallback=act_record["label_text"],
                )
                return executed

            elif action == "extract_data":
                act_record["label_text"] = act_record.get("label_text") or target_desc or "Extracted Data"
                act_record["extraction_field"] = act_record.get("extraction_field") or "extracted_data"
                act_record["transform"] = act_record.get("transform") or "trim_string"
                act_record["xpath"] = act_record.get("xpath") or "//table//tbody/tr[1]/td[5]"

                # Read text via SurfaceDriver semantic extraction adhering to Law of Demeter (INV-03)
                extracted_text = await self.driver.extract_text(
                    selector=hint or act_record["xpath"],
                    target_frame=target_frame_name,
                    timeout_ms=3000,
                )
                if extracted_text:
                    act_record["raw_extracted_value"] = PIIRedactor.redact_text(extracted_text)
                return True

        except Exception as e:
            clean_err = PIIRedactor.redact_text(str(e))[:100]
            self._log_event(f"Action execution error: {clean_err}")
            return False

        self._log_event(f"Unsupported action: {action}")
        return False

    async def _extract_visible_landmarks(self, target_frame: Optional[str] = None) -> str:
        """Extract lightweight text summary via SurfaceDriver in target frame context (INV-03, INV-06, INV-08, INV-09)."""
        try:
            landmarks_script = """
            () => {
                const candidates = Array.from(document.querySelectorAll('input, select, button, a, [role="button"]'));
                const visible = candidates.filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && r.top < window.innerHeight && r.bottom > 0;
                }).slice(0, 40);
                return visible.map(el => {
                    const tag = el.tagName.toLowerCase();
                    const name = el.getAttribute('name') || el.getAttribute('id') || '';
                    const isMasked = el.type === 'password' || /pin|ssn|token|secret|pass/i.test(name) || /pin|ssn|token|secret|password/i.test(el.getAttribute('placeholder') || '');
                    const val = isMasked ? '***' : (el.value || el.innerText || el.getAttribute('placeholder') || '').slice(0, 40);
                    return `<${tag} name='${name}'>${val}</${tag}>`;
                }).join('\\n');
            }
            """
            raw_landmarks = await self.driver.evaluate_script(landmarks_script, target_frame=target_frame)
            return PIIRedactor.redact_text(raw_landmarks) or "Standard layout page with form controls"
        except Exception:
            return "Interactive form controls present"

    def _log_event(self, msg: str) -> None:
        """Record timestamped event in discovery log with in-memory PII scrubbing (INV-27)."""
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        clean_msg = PIIRedactor.redact_text(str(msg))
        self.execution_log.append({"timestamp": timestamp, "message": clean_msg})

    def _export_evidence(self, artifact: CapabilityArtifact, raw_steps: List[Dict[str, Any]]) -> None:
        """Persist capability artifact and discovery audit log to /evidence/ with dual partitioning (INV-22, INV-28)."""
        timestamp = int(time.time())

        # 1. Export Capability Artifact JSON (Canonical + Timestamp Partitioned)
        artifact_dict = artifact.model_dump(mode="json")
        clean_artifact = PIIRedactor.redact_object(artifact_dict)
        clean_json = json.dumps(clean_artifact, indent=2)

        artifact_path = self.evidence_dir / "capability_artifact.json"
        artifact_path.write_text(clean_json, encoding="utf-8")

        safe_cap_id = re.sub(r"[^a-zA-Z0-9_-]", "_", artifact.metadata.capability_id)
        partitioned_path = self.evidence_dir / f"{safe_cap_id}_{timestamp}_artifact.json"
        partitioned_path.write_text(clean_json, encoding="utf-8")

        # 2. Export Discovery Run Log (Canonical + Timestamp Partitioned)
        log_lines = [f"[{entry['timestamp']}] {entry['message']}" for entry in self.execution_log]
        clean_log = PIIRedactor.redact_text("\n".join(log_lines))

        log_path = self.evidence_dir / "discovery_run.log"
        log_path.write_text(clean_log, encoding="utf-8")

        partitioned_log_path = self.evidence_dir / f"discovery_run_{timestamp}.log"
        partitioned_log_path.write_text(clean_log, encoding="utf-8")
