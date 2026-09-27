"""Level 2 Integration Tests: Playwright Surface Driver, SoM, and Locator Resolution against Live Mock Bank."""

import threading
import time
import pytest
import uvicorn

from mock_bank.server import app
from src.guardrails.allowlist import RouteAllowlist
from src.schemas.artifact import LocatorCascade, LocatorStrategyType, LocatorTier
from src.surface.driver import SurfaceDriver
from src.surface.locator_engine import LocatorEngine
from src.surface.som_annotator import SoMAnnotator
from typing import Any

TEST_PORT = 8124
SERVER_URL = f"http://127.0.0.1:{TEST_PORT}"


@pytest.fixture(scope="module")
def live_mock_server() -> Any:
    """Start uvicorn mock bank server in background thread for live surface testing."""
    config = uvicorn.Config(app, host="127.0.0.1", port=TEST_PORT, log_level="error", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(0.6)  # Allow server to bind
    yield SERVER_URL
    server.should_exit = True


@pytest.mark.asyncio
async def test_surface_driver_and_locator_resolution(live_mock_server: str) -> None:
    """Verify live Chromium navigation, label proximity resolution on hostile tables, and form submission."""
    allowlist = RouteAllowlist([f"127.0.0.1:{TEST_PORT}", f"localhost:{TEST_PORT}"])

    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        # Navigate to member lookup
        await driver.navigate(f"{live_mock_server}/servicing/lookup")

        # Test Set-of-Marks injection
        marks = await SoMAnnotator.inject_marks(driver.page)
        assert len(marks) >= 2  # Input and submit button at minimum
        await SoMAnnotator.clear_marks(driver.page)

        # Resolve unlabelled input via Label Proximity cascade (hostile table testing)
        input_cascade = LocatorCascade(
            tiers=[
                LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role="textbox", name="Member ID"),
                LocatorTier(strategy=LocatorStrategyType.LABEL_PROXIMITY, label_text="Member ID:", direction="right"),
            ],
            robustness_rationale="Resolves dynamic ASP.NET textbox by proximity to preceding table label.",
        )

        input_loc, tier_used, elapsed_ms = await LocatorEngine.resolve(driver.page, input_cascade)
        assert input_loc is not None
        assert elapsed_ms < 1000  # Sub-second resolution

        # Fill input
        await input_loc.fill("1042")

        # Resolve Submit button
        btn_cascade = LocatorCascade(
            tiers=[
                LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role="button", name="Lookup Member"),
                LocatorTier(strategy=LocatorStrategyType.STRUCTURAL_XPATH, xpath="//input[@value='Lookup Member']"),
            ],
            robustness_rationale="Matches submit button by accessible name or fallback value attribute.",
        )

        btn_loc, btn_tier, _ = await LocatorEngine.resolve(driver.page, btn_cascade)
        assert btn_loc is not None
        await btn_loc.click()

        # Await results table
        await driver.page.wait_for_selector("#ctl00_Main_gvMembers", timeout=3000)
        content = await driver.page.content()
        assert "Jane Doe" in content
        assert "4850.25" in content or "4,850.25" in content
