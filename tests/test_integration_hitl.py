"""Integration tests for In-Situ HITL live session handoff seam."""

import asyncio
import threading
import time
from pathlib import Path
import pytest
import uvicorn
from mock_bank.server import app
from src.surface.driver import SurfaceDriver
from src.guardrails.allowlist import RouteAllowlist
from src.hitl.handoff import InSituHandoffManager
from src.schemas.hitl import HumanInterventionRecord

HITL_PORT = 8126
HITL_SERVER_URL = f"http://127.0.0.1:{HITL_PORT}"


@pytest.fixture(scope="module")
def live_hitl_server():
    """Start dedicated mock bank server for HITL integration tests."""
    config = uvicorn.Config(app, host="127.0.0.1", port=HITL_PORT, log_level="error", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(0.6)
    yield HITL_SERVER_URL
    server.should_exit = True


@pytest.mark.asyncio
async def test_in_situ_hitl_handoff_lifecycle(live_hitl_server: str, tmp_path: Path) -> None:
    """Verify live session pause, overlay injection, operator action capture, and clean resumption."""
    allowlist = RouteAllowlist([f"127.0.0.1:{HITL_PORT}", f"localhost:{HITL_PORT}"])

    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        await driver.navigate(f"{live_hitl_server}/servicing/lookup")
        page = driver.page

        # Verify baseline page loaded
        assert "Member Search" in await page.title()

        handoff_mgr = InSituHandoffManager(driver=driver, evidence_dir=tmp_path)

        # 1. Initiate handoff asynchronously
        handoff_task = asyncio.create_task(
            handoff_mgr.initiate_handoff(
                capability_id="lookup_savings_balance",
                trigger_reason="Simulated unhandled confirmation dialog",
                timeout_seconds=10,
            )
        )

        # Allow injection
        await asyncio.sleep(0.5)

        # 2. Verify visual dock is injected into live DOM
        dock_count = await page.locator("#__interface_ai_hitl_dock__").count()
        assert dock_count == 1, "In-situ dock overlay must be injected into live page DOM"

        # 3. Simulate human operator action: click on the search input and type
        input_elem = page.locator("input[type='text']").first
        await input_elem.click()
        await input_elem.fill("2088")

        # Give event listener a moment to dispatch event
        await asyncio.sleep(0.2)

        # 4. Simulate operator clicking 'Resume Automation' in the visual dock
        resume_btn = page.locator("#__interface_ai_resume_btn__")
        assert await resume_btn.count() == 1
        await resume_btn.click()

        # 5. Await handoff completion
        record = await asyncio.wait_for(handoff_task, timeout=5.0)

        # 6. Assertions on completed intervention record
        assert isinstance(record, HumanInterventionRecord)
        assert record.capability_id == "lookup_savings_balance"
        assert record.trigger_context["reason"] == "Simulated unhandled confirmation dialog"

        # Pre & Post screenshot validation
        assert Path(record.pre_state["screenshot"]).exists()
        assert Path(record.post_state["screenshot"]).exists()

        # Dock must be removed from page DOM after resumption
        remaining_dock = await page.locator("#__interface_ai_hitl_dock__").count()
        assert remaining_dock == 0, "Dock overlay must be cleaned up from page after resumption"

        # Live browser session must remain responsive
        assert await page.title() != ""
        assert (tmp_path / "human_intervention.json").exists()
