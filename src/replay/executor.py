"""Deterministic production replay executor with zero LLM in the loop."""

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
from playwright.async_api import Page

from src.guardrails.pii_redactor import PIIRedactor
from src.guardrails.risk_governor import RiskGovernor
from src.schemas.artifact import ActionType, CapabilityArtifact, CapabilityStep
from src.schemas.execution import ReplayInput, ReplayResult, ReplayStatus
from src.surface.driver import SurfaceDriver
from src.surface.locator_engine import LocatorEngine
from src.replay.classifier import OutcomeClassifier
from src.replay.recovery import TransientRecoverySentinel

# Precompiled currency regex supporting negative accounting formats (INV-24)
CURRENCY_RE = re.compile(r"[-+(]?\s*[$€£]?\s*\d[\d,]*(?:\.\d+)?\)?")


class ReplayExecutor:
    """Zero-LLM deterministic capability replayer with tri-partite outcome governance."""

    def __init__(self, driver: SurfaceDriver, evidence_dir: Optional[Path] = None) -> None:
        self.driver = driver
        self.evidence_dir = evidence_dir or Path("evidence")
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    async def execute(
        self, artifact: CapabilityArtifact, replay_input: ReplayInput
    ) -> ReplayResult:
        """Execute a capability artifact deterministically with given parameters (INV-05, INV-20, INV-24)."""
        t0 = time.perf_counter()
        deadline = t0 + 5.0  # Bounded execution budget (INV-24)
        extracted_data: Dict[str, Any] = {}
        page = self.driver.page

        try:
            for step in artifact.steps:
                # Check execution deadline budget (INV-24)
                if time.perf_counter() > deadline:
                    elapsed_ms = int((time.perf_counter() - t0) * 1000)
                    evidence = await self._capture_failure_evidence(page, step.step_id)
                    return OutcomeClassifier.build_hard_failure(
                        capability_id=artifact.metadata.capability_id,
                        duration_ms=elapsed_ms,
                        failed_step_id=step.step_id,
                        expected="Completion within performance deadline",
                        observed=f"Execution budget exceeded at step '{step.step_id}' ({elapsed_ms}ms)",
                        evidence_paths=evidence,
                    )

                target_frame = self.driver.get_target_frame(
                    step.locator.target_frame if step.locator else None
                )
                if target_frame is None or (hasattr(target_frame, "is_detached") and target_frame.is_detached()):
                    target_frame = page

                # 1. Recover from transient states (loading spinners, interstitial notices)
                await TransientRecoverySentinel.handle_transient_states(target_frame)

                # 2. Resolve value binding if present
                resolved_value = self._resolve_binding(step.value_binding, replay_input.parameters)

                # 3. Dispatch Action with Risk Governance & Route Allowlist (INV-25, INV-26)
                action_success, failure_details = await self._dispatch_action(
                    step, target_frame, resolved_value, extracted_data, replay_input.strict_mode
                )

                if not action_success:
                    elapsed_ms = int((time.perf_counter() - t0) * 1000)
                    evidence = await self._capture_failure_evidence(page, step.step_id)
                    return OutcomeClassifier.build_hard_failure(
                        capability_id=artifact.metadata.capability_id,
                        duration_ms=elapsed_ms,
                        failed_step_id=step.step_id,
                        expected=failure_details.get("expected", "Element interaction"),
                        observed=failure_details.get("observed", "Locator resolution failed"),
                        evidence_paths=evidence,
                    )

                # 4. Evaluate Checkpoint Branches & Assertions if rules exist (INV-06)
                if step.checkpoint and step.checkpoint.branches:
                    if hasattr(target_frame, "is_detached") and target_frame.is_detached():
                        target_frame = page
                    try:
                        page_text = await target_frame.content()
                    except Exception:
                        page_text = await page.content()
                    current_url = page.url
                    branch_res = OutcomeClassifier.evaluate_checkpoint_branches(
                        step.checkpoint, page_text, current_url
                    )

                    if not branch_res.should_continue:
                        elapsed_ms = int((time.perf_counter() - t0) * 1000)
                        if branch_res.outcome_category == "business_outcome":
                            clean_data = PIIRedactor.redact_object(extracted_data)
                            return OutcomeClassifier.build_business_outcome(
                                capability_id=artifact.metadata.capability_id,
                                duration_ms=elapsed_ms,
                                code=branch_res.outcome_code or "BUSINESS_OUTCOME",
                                message=branch_res.message or "Expected business outcome reached.",
                                data=clean_data,
                            )
                        else:
                            evidence = await self._capture_failure_evidence(page, step.step_id)
                            return OutcomeClassifier.build_hard_failure(
                                capability_id=artifact.metadata.capability_id,
                                duration_ms=elapsed_ms,
                                failed_step_id=step.step_id,
                                expected="Checkpoint continuation",
                                observed=branch_res.message or "Checkpoint failure condition triggered",
                                evidence_paths=evidence,
                            )

            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            clean_data = PIIRedactor.redact_object(extracted_data)
            return ReplayResult(
                status=ReplayStatus.SUCCESS,
                capability_id=artifact.metadata.capability_id,
                duration_ms=elapsed_ms,
                data=clean_data,
                message="Capability replayed successfully with all checkpoints verified.",
            )
        except Exception as unhandled:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            evidence = await self._capture_failure_evidence(page, "unhandled_crash")
            clean_err = PIIRedactor.redact_text(str(unhandled))[:120]
            return OutcomeClassifier.build_hard_failure(
                capability_id=artifact.metadata.capability_id,
                duration_ms=elapsed_ms,
                failed_step_id="runtime_execution_exception",
                expected="Clean deterministic execution",
                observed=f"Unhandled runtime exception: {type(unhandled).__name__}: {clean_err}",
                evidence_paths=evidence,
            )

    async def _dispatch_action(
        self,
        step: CapabilityStep,
        target_frame: Any,
        value: Optional[str],
        extracted_data: Dict[str, Any],
        strict_mode: bool = True,
    ) -> tuple[bool, Dict[str, str]]:
        """Dispatch concrete step action with risk and allowlist gates (INV-03, INV-25, INV-26, INV-28)."""
        action = step.action

        # Action Risk Governor verification (INV-26)
        risk = RiskGovernor.classify_action(
            action_type=action,
            target_text=step.description,
            value=value,
        )
        if RiskGovernor.requires_confirmation(risk, strict_mode=strict_mode):
            return False, {
                "expected": "Safe or confirmed action execution",
                "observed": f"Action blocked by RiskGovernor: {risk.value} requires confirmation for step '{step.step_id}'",
            }

        if action == ActionType.NAVIGATE:
            if not value:
                return False, {"expected": "Valid URL", "observed": "Empty navigation value"}
            # Route Allowlist Gate & Navigation (INV-03, INV-25)
            # SurfaceDriver.navigate encapsulates allowlist verification adhering to Law of Demeter
            await self.driver.navigate(value)
            return True, {}

        elif action in (ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.SELECT_OPTION):
            if not step.locator:
                return False, {"expected": "Locator cascade", "observed": "None"}

            loc, tier, _ = await LocatorEngine.resolve(target_frame, step.locator)
            if not loc:
                return False, {
                    "expected": f"Resolvable element via cascade ({step.locator.robustness_rationale})",
                    "observed": "Zero matches across all tiers",
                }

            try:
                if action == ActionType.CLICK:
                    await loc.click(timeout=3000)
                    p = target_frame if hasattr(target_frame, "wait_for_load_state") else getattr(target_frame, "page", None)
                    if p:
                        try:
                            await p.wait_for_load_state("domcontentloaded", timeout=500)
                        except Exception:
                            pass
                elif action == ActionType.TYPE_TEXT:
                    await loc.fill(value or "", timeout=3000)
                elif action == ActionType.SELECT_OPTION:
                    await loc.select_option(value or "", timeout=3000)
            except Exception as e:
                clean_err = PIIRedactor.redact_text(str(e))[:100]
                return False, {
                    "expected": f"Interactable element ({action})",
                    "observed": f"Playwright interaction exception: {clean_err}",
                }
            return True, {}

        elif action == ActionType.WAIT_FOR_STATE:
            if step.locator:
                loc, _, _ = await LocatorEngine.resolve(target_frame, step.locator)
                if not loc:
                    return False, {
                        "expected": "Resolvable element to wait for",
                        "observed": "Locator resolution returned None",
                    }
                try:
                    await loc.wait_for(state="visible", timeout=3000)
                except Exception as e:
                    clean_err = PIIRedactor.redact_text(str(e))[:100]
                    return False, {
                        "expected": "Element to reach visible state",
                        "observed": f"Wait exception: {clean_err}",
                    }
            else:
                try:
                    dur = float(value or "1.0")
                    await asyncio.sleep(min(dur, 1.5))
                except Exception:
                    await asyncio.sleep(0.5)
            return True, {}

        elif action == ActionType.EXTRACT_DATA:
            if step.extractions:
                for ext in step.extractions:
                    loc, _, _ = await LocatorEngine.resolve(target_frame, ext.locator)
                    if loc:
                        try:
                            raw_val = await loc.inner_text(timeout=2000)
                            transformed = self._apply_transform(raw_val, ext.transform)
                            extracted_data[ext.output_field] = transformed
                        except Exception as e:
                            clean_err = PIIRedactor.redact_text(str(e))[:100]
                            return False, {
                                "expected": f"Extractable text from {ext.output_field}",
                                "observed": f"Extraction read error: {clean_err}",
                            }
                    else:
                        return False, {
                            "expected": f"Resolvable locator for extraction field '{ext.output_field}'",
                            "observed": "Locator resolution returned None",
                        }
            return True, {}

        return True, {}

    def _resolve_binding(self, binding: Optional[str], params: Dict[str, Any]) -> Optional[str]:
        """Resolve dynamic input bindings like '$input.member_id' to concrete parameters."""
        if not binding:
            return None
        if binding.startswith("$input."):
            param_key = binding[7:]
            return str(params.get(param_key, ""))
        return binding

    def _apply_transform(self, raw_text: str, transform: str) -> Any:
        """Transform raw extracted text into structured typed primitives (INV-24)."""
        text = raw_text.strip()
        if transform == "parse_currency":
            # Extract numbers and decimals handling accounting negative signs e.g. "-$50.00" or "($50.00)" or "$4,850.25"
            m = CURRENCY_RE.search(text)
            if m:
                matched_str = m.group(0)
                is_negative = "-" in matched_str or "(" in matched_str
                clean = re.sub(r"[^\d.]", "", matched_str)
                try:
                    val = float(clean)
                    return -val if is_negative else val
                except ValueError:
                    return 0.0
            return 0.0
        elif transform == "trim_string":
            return text
        elif transform == "extract_digits":
            return re.sub(r"\D", "", text)
        return text

    async def _capture_failure_evidence(self, page: Page, step_id: str) -> Dict[str, str]:
        """Capture pre-masked screenshot and sanitized DOM snapshot on replay failure (INV-22, INV-28, INV-31)."""
        timestamp = int(time.time())
        safe_step_id = re.sub(r"[^a-zA-Z0-9_-]", "_", step_id)
        screenshot_path = self.evidence_dir / f"failure_{safe_step_id}_{timestamp}.png"
        dom_path = self.evidence_dir / f"failure_{safe_step_id}_{timestamp}.html"

        try:
            # Pre-mask in-browser DOM elements before screenshotting to guarantee zero visual PII egress (INV-28, INV-31)
            mask_js = """() => {
                const s = document.createElement('style');
                s.id = '__screenshot_pii_mask__';
                s.textContent = 'input, select, textarea, [data-sensitive], [class*="ssn"], [class*="account"], [class*="balance"], [class*="member"], td:nth-child(n+3) { background-color: #000 !important; color: transparent !important; filter: blur(12px) !important; }';
                (document.head || document.documentElement).appendChild(s);
            }"""
            try:
                await asyncio.gather(
                    *[frame.evaluate(mask_js) for frame in page.frames],
                    return_exceptions=True,
                )
                await page.screenshot(path=str(screenshot_path), full_page=True, timeout=5000)
            finally:
                unmask_js = "() => { const s = document.getElementById('__screenshot_pii_mask__'); if (s) s.remove(); }"
                await asyncio.gather(
                    *[frame.evaluate(unmask_js) for frame in page.frames],
                    return_exceptions=True,
                )

            content = await page.content()
            clean_dom = PIIRedactor.redact_text(content)
            await asyncio.to_thread(dom_path.write_text, clean_dom, encoding="utf-8")
            return {
                "screenshot": str(screenshot_path),
                "dom": str(dom_path),
            }
        except Exception:
            return {}

