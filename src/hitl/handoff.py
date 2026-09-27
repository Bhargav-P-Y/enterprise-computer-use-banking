"""In-Situ Human-in-the-Loop (HITL) Live Session Handoff Manager.

Enables seamless pause and operator takeover within the EXACT SAME active browser session,
capturing dual-stream evidence (DOM actions + state delta) with zero PII leakage.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page
from src.surface.driver import SurfaceDriver
from src.schemas.hitl import HumanInterventionRecord, RecordedAction
from src.hitl.diff_engine import StateDiffEngine
from src.guardrails.pii_redactor import PIIRedactor


class InSituHandoffManager:
    """Manages live session handoff to a human operator."""

    def __init__(self, driver: SurfaceDriver, evidence_dir: Optional[Path] = None) -> None:
        self.driver = driver
        self.evidence_dir = evidence_dir or Path("evidence")
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.recorded_actions: List[RecordedAction] = []
        self._resume_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._recorder_js_path = Path(__file__).parent / "event_recorder.js"
        # Pre-cache recorder script in memory to eliminate blocking sync I/O in async loop (INV-06)
        self._recorder_code: str = ""
        if self._recorder_js_path.exists():
            self._recorder_code = self._recorder_js_path.read_text(encoding="utf-8")

    async def initiate_handoff(
        self,
        capability_id: str,
        trigger_reason: str,
        context: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = 180,
    ) -> HumanInterventionRecord:
        """Pause automation on active page, hand control to human, record actions, and resume (INV-28, INV-31, INV-32)."""
        async with self._lock:
            self._loop = asyncio.get_running_loop()
            page: Page = self.driver.page
            intervention_id = f"hitl_{int(time.time())}"
            t_start_monotonic = time.monotonic()
            self.recorded_actions.clear()
            self._resume_event.clear()

            # Helper for screenshot with visual PII masking on inputs, balance cells, and sensitive elements (INV-28, INV-31)
            async def _capture_safe_screenshot(path: Path) -> None:
                mask_style_id = "__hitl_screenshot_pii_mask__"
                blur_css = (
                    'input[type="password"], input[name*="ssn" i], input[name*="dob" i], '
                    'input[name*="account" i], table td.balance, [class*="balance" i], '
                    '[class*="account" i], [class*="ssn" i], [id*="balance" i], [id*="ssn" i], '
                    '.pii-sensitive { filter: blur(8px) !important; }'
                )
                try:
                    for f in page.frames:
                        try:
                            await asyncio.wait_for(
                                f.evaluate(f"""(() => {{
                                    if (!document.getElementById('{mask_style_id}')) {{
                                        const s = document.createElement('style');
                                        s.id = '{mask_style_id}';
                                        s.textContent = '{blur_css}';
                                        const target = document.head || document.documentElement;
                                        if (target) target.appendChild(s);
                                    }}
                                }})()"""),
                                timeout=2.0,
                            )
                        except Exception:
                            pass
                    await page.screenshot(path=str(path), full_page=True, timeout=5000)
                except Exception:
                    pass
                finally:
                    for f in page.frames:
                        try:
                            await f.evaluate(f"(() => {{ const s = document.getElementById('{mask_style_id}'); if (s) s.remove(); }})()")
                        except Exception:
                            pass

            # 1. Capture Pre-Intervention State with URL scrubbing and timeout bounds (INV-07, INV-27)
            pre_url = PIIRedactor.redact_text(page.url)
            pre_dom_raw = await asyncio.wait_for(page.content(), timeout=5.0)
            pre_dom = await asyncio.to_thread(PIIRedactor.redact_text, pre_dom_raw)
            pre_screenshot_path = self.evidence_dir / f"{intervention_id}_pre.png"
            await _capture_safe_screenshot(pre_screenshot_path)

            pre_state = {
                "url": pre_url,
                "screenshot": str(pre_screenshot_path),
                "timestamp_ms": int(time.time() * 1000),
            }

            # 2. Expose Event & Resume Bindings to Page (if not already exposed)
            async def _handle_recorded_event(source: Any, payload_str: str) -> None:
                try:
                    if len(self.recorded_actions) >= 2000:
                        return
                    data = json.loads(payload_str)
                    raw_val = data.get("value") or data.get("target_text") or ""
                    safe_val = "[REDACTED_SENSITIVE_INPUT]" if data.get("is_sensitive", False) else PIIRedactor.redact_text(str(raw_val))
                    action = RecordedAction(
                        sequence=len(self.recorded_actions) + 1,
                        action_type=data.get("type", "unknown"),
                        target={
                            "tag": PIIRedactor.redact_text(str(data.get("target_tag") or "")),
                            "id": PIIRedactor.redact_text(str(data.get("target_id") or "")),
                            "name": PIIRedactor.redact_text(str(data.get("target_name") or "")),
                            "selector": PIIRedactor.redact_text(str(data.get("selector") or "")),
                            "coordinates": data.get("coordinates"),
                        },
                        value=safe_val,
                        is_masked=data.get("is_sensitive", False),
                        timestamp_ms=data.get("timestamp", int(time.time() * 1000)),
                    )
                    self.recorded_actions.append(action)
                except Exception:
                    pass

            async def _handle_resume_signal(source: Any = None) -> None:
                self._resume_event.set()

            try:
                await page.expose_binding("__interface_ai_record_event__", _handle_recorded_event)
            except Exception:
                pass  # Binding may already exist on reused page

            try:
                await page.expose_binding("__interface_ai_resume_signal__", _handle_resume_signal)
            except Exception:
                pass

            # 3. Inject In-Situ Recorder & Dock Overlay (add_init_script for reload resilience - INV-31)
            recorder_code = self._recorder_code or (self._recorder_js_path.read_text(encoding="utf-8") if self._recorder_js_path.exists() else "")
            try:
                await page.add_init_script(recorder_code)
            except Exception:
                pass
            await asyncio.wait_for(page.evaluate(recorder_code), timeout=5.0)

            # 4. Await Human Operator Completion (Dock click, manual resume, or timeout)
            clean_resumption = True
            try:
                await asyncio.wait_for(self._resume_event.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                clean_resumption = False  # Handoff timed out; proceed with recovery/post-check

            # 5. Capture Post-Intervention State with URL scrubbing and timeout bounds (INV-07, INV-27)
            page_closed = False
            try:
                if page.is_closed():
                    page_closed = True
                    post_url = pre_url
                    post_dom = ""
                else:
                    post_url = PIIRedactor.redact_text(page.url)
                    post_dom_raw = await asyncio.wait_for(page.content(), timeout=5.0)
                    post_dom = await asyncio.to_thread(PIIRedactor.redact_text, post_dom_raw)
            except Exception:
                page_closed = True
                post_url = pre_url
                post_dom = ""

            post_screenshot_path = self.evidence_dir / f"{intervention_id}_post.png"
            if not page_closed:
                try:
                    await _capture_safe_screenshot(post_screenshot_path)
                except Exception:
                    pass

            post_state = {
                "url": post_url,
                "screenshot": str(post_screenshot_path),
                "timestamp_ms": int(time.time() * 1000),
            }

            # 6. Compute State Delta
            state_delta = await asyncio.to_thread(
                StateDiffEngine.compute_state_delta,
                pre_url=pre_url,
                post_url=post_url,
                pre_dom=pre_dom,
                post_dom=post_dom,
                recorded_actions=list(self.recorded_actions),
            )

            # 7. Clean up visual dock and deactivate listener processing (INV-32)
            try:
                cleanup_script = """(() => {
                    window.__interface_ai_recorder_active = false;
                    const dock = document.getElementById('__interface_ai_hitl_dock__');
                    if (dock) dock.remove();
                })()"""
                await asyncio.wait_for(page.evaluate(cleanup_script), timeout=5.0)
            except Exception:
                pass

            duration_ms = max(0, int((time.monotonic() - t_start_monotonic) * 1000))

            safe_reason = PIIRedactor.redact_text(trigger_reason)
            safe_context_details = PIIRedactor.redact_dict(context or {})

            record = HumanInterventionRecord(
                intervention_id=intervention_id,
                capability_id=capability_id,
                trigger_context={"reason": safe_reason, "details": safe_context_details},
                pre_state=pre_state,
                recorded_actions=self.recorded_actions,
                post_state=post_state,
                state_delta=state_delta,
                audit_metadata={
                    "operator_handoff_duration_ms": duration_ms,
                    "actions_captured": len(self.recorded_actions),
                    "clean_resumption": clean_resumption,
                    "page_closed_by_operator": page_closed,
                },
            )

            # 8. Export structured evidence (preserving timestamped audit history + canonical latest)
            session_evidence_file = self.evidence_dir / f"{intervention_id}.json"
            canonical_evidence_file = self.evidence_dir / "human_intervention.json"
            await asyncio.to_thread(StateDiffEngine.export_intervention_evidence, record, session_evidence_file)
            await asyncio.to_thread(StateDiffEngine.export_intervention_evidence, record, canonical_evidence_file)

            return record

    def resume(self) -> None:
        """Programmatically resume from Python/CLI with cross-thread event loop safety."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._resume_event.set)
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(self._resume_event.set)
            except RuntimeError:
                self._resume_event.set()
