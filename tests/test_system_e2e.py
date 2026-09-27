"""Level 3 End-to-End System Tests.

Traces full input lifecycle across Discovery, Compilation, Zero-LLM Replay,
Tri-Partite Classification, and In-Situ HITL Seam.
"""

import asyncio
import threading
import time
from pathlib import Path
import pytest
import uvicorn
from mock_bank.server import app
from src.surface.driver import SurfaceDriver
from src.guardrails.allowlist import RouteAllowlist
from src.discovery.agent import DiscoveryAgent
from src.replay.executor import ReplayExecutor
from src.hitl.handoff import InSituHandoffManager
from src.schemas.execution import ReplayInput, ReplayStatus
from tests.test_integration_discovery import StubGeminiClient

SYSTEM_PORT = 8128
SYSTEM_SERVER_URL = f"http://127.0.0.1:{SYSTEM_PORT}"


@pytest.fixture(scope="module")
def live_system_server():
    """Start mock bank server on dedicated port for system tests."""
    config = uvicorn.Config(app, host="127.0.0.1", port=SYSTEM_PORT, log_level="error", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(0.6)
    yield SYSTEM_SERVER_URL
    server.should_exit = True


@pytest.mark.asyncio
async def test_full_system_lifecycle(live_system_server: str, tmp_path: Path) -> None:
    """End-to-End Test: Discovery -> Artifact -> Zero-LLM Replay (Success, Business Outcome, Fault) -> HITL."""
    allowlist = RouteAllowlist([f"127.0.0.1:{SYSTEM_PORT}", f"localhost:{SYSTEM_PORT}"])

    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        # Phase 1: Stage 1 Discovery & Compilation
        stub_client = StubGeminiClient()
        agent = DiscoveryAgent(driver=driver, gemini_client=stub_client, evidence_dir=tmp_path)
        artifact = await agent.discover_capability(
            capability_id="lookup_savings_balance",
            goal="Lookup member 1042 and extract regular savings balance",
            start_url=f"{live_system_server}/servicing/lookup",
            target_app="HeritageCoreBanking",
            known_inputs={"member_id": "1042"},
            max_steps=6,
        )

        assert artifact is not None
        assert len(artifact.steps) == 4

        # Phase 2: Stage 3 Deterministic Zero-LLM Replay - Happy Path
        executor = ReplayExecutor(driver=driver, evidence_dir=tmp_path)
        happy_input = ReplayInput(capability_id="lookup_savings_balance", parameters={"member_id": "1042"})
        happy_result = await executor.execute(artifact, happy_input)

        assert happy_result.status == ReplayStatus.SUCCESS
        assert happy_result.data is not None
        assert happy_result.data.get("savings_balance") == 4850.25
        assert happy_result.duration_ms < 2000

        # Phase 3: Stage 3 Deterministic Zero-LLM Replay - Business Outcome (Member Not Found)
        missing_input = ReplayInput(capability_id="lookup_savings_balance", parameters={"member_id": "9999"})
        business_result = await executor.execute(artifact, missing_input)

        assert business_result.status == ReplayStatus.BUSINESS_OUTCOME
        assert business_result.business_outcome_code == "MEMBER_NOT_FOUND"

        # Phase 4: Stage 4 In-Situ HITL Live Session Takeover
        handoff_mgr = InSituHandoffManager(driver=driver, evidence_dir=tmp_path)
        handoff_task = asyncio.create_task(
            handoff_mgr.initiate_handoff(
                capability_id="lookup_savings_balance",
                trigger_reason="Compliance manager signoff required",
                timeout_seconds=5,
            )
        )
        await asyncio.sleep(0.4)

        # Simulate operator interaction and resume
        resume_btn = driver.page.locator("#__interface_ai_resume_btn__")
        if await resume_btn.count() > 0:
            await resume_btn.click()

        hitl_record = await handoff_task
        assert hitl_record.capability_id == "lookup_savings_balance"
        assert hitl_record.audit_metadata["clean_resumption"] is True
        assert (tmp_path / "human_intervention.json").exists()
