"""Live End-to-End Pipeline Runner.

Runs real discovery with Gemini 3.8 Flash against the live mock bank server,
exports evidence/capability_artifact.json, runs deterministic replays,
and exports all /evidence/ artifacts.
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

# Ensure workspace root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn
from mock_bank.server import app
from src.surface.driver import SurfaceDriver
from src.guardrails.allowlist import RouteAllowlist
from src.discovery.agent import DiscoveryAgent
from src.discovery.gemini_client import DiscoveryGeminiClient
from src.replay.executor import ReplayExecutor
from src.hitl.handoff import InSituHandoffManager
from src.schemas.execution import ReplayInput

PORT = 8123
SERVER_URL = f"http://127.0.0.1:{PORT}"
EVIDENCE_DIR = Path("evidence")


def start_server() -> uvicorn.Server:
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(0.8)
    return server


async def run_live_pipeline() -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    server = start_server()
    allowlist = RouteAllowlist([f"127.0.0.1:{PORT}", f"localhost:{PORT}"])

    print("=================================================================")
    print("STAGE 1: RUNNING LIVE MULTIMODAL DISCOVERY WITH GEMINI 3.8 FLASH")
    print("=================================================================")
    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        gemini_client = DiscoveryGeminiClient()
        agent = DiscoveryAgent(driver=driver, gemini_client=gemini_client, evidence_dir=EVIDENCE_DIR)

        goal = "Lookup member 1042 and extract regular savings balance"
        start_url = f"{SERVER_URL}/servicing/lookup"

        artifact = await agent.discover_capability(
            capability_id="lookup_savings_balance",
            goal=goal,
            start_url=start_url,
            target_app="HeritageCoreBanking",
            known_inputs={"member_id": "1042"},
            max_steps=6,
        )

        print(f"Discovery Complete! Compiled {len(artifact.steps)} steps.")
        print(f"Artifact exported to: {EVIDENCE_DIR / 'capability_artifact.json'}")
        print(f"Trace log exported to: {EVIDENCE_DIR / 'discovery_run.log'}")

    print("\n=================================================================")
    print("STAGE 3: DETERMINISTIC REPLAY RUNS (ZERO-LLM FIREWALL)")
    print("=================================================================")
    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        executor = ReplayExecutor(driver=driver, evidence_dir=EVIDENCE_DIR)

        # 1. Happy Path Replay (Member 1042)
        print("\n--- Running Replay Happy Path (Member 1042) ---")
        happy_input = ReplayInput(capability_id="lookup_savings_balance", parameters={"member_id": "1042"})
        happy_res = await executor.execute(artifact, happy_input)
        print(f"Status: {happy_res.status.value.upper()} | Duration: {happy_res.duration_ms}ms | Data: {happy_res.data}")
        (EVIDENCE_DIR / "replay_success.log").write_text(
            f"Status: {happy_res.status.value}\nDuration: {happy_res.duration_ms}ms\nData: {json.dumps(happy_res.data, indent=2)}\nMessage: {happy_res.message}\n",
            encoding="utf-8",
        )

        # 2. Business Outcome Replay (Member 9999 - Missing Member)
        print("\n--- Running Replay Business Outcome (Member 9999) ---")
        missing_input = ReplayInput(capability_id="lookup_savings_balance", parameters={"member_id": "9999"})
        business_res = await executor.execute(artifact, missing_input)
        print(f"Status: {business_res.status.value.upper()} | Code: {business_res.business_outcome_code} | Message: {business_res.message}")
        (EVIDENCE_DIR / "replay_business_outcome.log").write_text(
            f"Status: {business_res.status.value}\nCode: {business_res.business_outcome_code}\nMessage: {business_res.message}\nDuration: {business_res.duration_ms}ms\n",
            encoding="utf-8",
        )

        # 3. Hard Failure Replay (Fault Injection on unhandled failure endpoint)
        print("\n--- Running Replay Fault Injection (Unhandled Error Page) ---")
        fault_artifact = artifact.model_copy(deep=True)
        fault_artifact.steps[0].value_binding = f"{SERVER_URL}/servicing/unhandled_crash"
        fault_res = await executor.execute(fault_artifact, happy_input)
        print(f"Status: {fault_res.status.value.upper()} | Failure Context: {fault_res.failure_context}")
        (EVIDENCE_DIR / "replay_failure.log").write_text(
            f"Status: {fault_res.status.value}\nFailure Context: {json.dumps(fault_res.failure_context, indent=2)}\nMessage: {fault_res.message}\n",
            encoding="utf-8",
        )

    print("\n=================================================================")
    print("STAGE 4: IN-SITU HITL LIVE SESSION HANDOFF DEMO")
    print("=================================================================")
    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        await driver.navigate(f"{SERVER_URL}/servicing/lookup")
        mgr = InSituHandoffManager(driver=driver, evidence_dir=EVIDENCE_DIR)

        handoff_task = asyncio.create_task(
            mgr.initiate_handoff(
                capability_id="lookup_savings_balance",
                trigger_reason="Simulated supervisor signoff on high-value inquiry",
                timeout_seconds=5,
            )
        )
        await asyncio.sleep(0.5)

        # Simulate operator interaction
        input_elem = driver.page.locator("input[type='text']").first
        if await input_elem.count() > 0:
            await input_elem.fill("3011")
        resume_btn = driver.page.locator("#__interface_ai_resume_btn__")
        if await resume_btn.count() > 0:
            await resume_btn.click()

        hitl_rec = await handoff_task
        print(f"HITL Handoff Recorded: {len(hitl_rec.recorded_actions)} operator actions.")
        print(f"Evidence exported to: {EVIDENCE_DIR / 'human_intervention.json'}")

    server.should_exit = True
    print("\nAll live pipeline stages finished successfully!")


if __name__ == "__main__":
    asyncio.run(run_live_pipeline())
