"""Integration tests for Discovery Agent and Compiler."""

import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
import pytest
import uvicorn
from mock_bank.server import app
from src.surface.driver import SurfaceDriver
from src.guardrails.allowlist import RouteAllowlist
from src.discovery.agent import DiscoveryAgent
from src.discovery.gemini_client import DiscoveryGeminiClient
from src.schemas.artifact import CapabilityArtifact

DISCOVERY_PORT = 8127
DISCOVERY_SERVER_URL = f"http://127.0.0.1:{DISCOVERY_PORT}"


class StubGeminiClient(DiscoveryGeminiClient):
    """Stubbed Gemini client returning scripted decision sequence for deterministic test runs."""

    def __init__(self) -> None:
        self.step = 0
        self.decisions = [
            # Decision for Step 2: input member ID
            {
                "thought": "I observe the member search console with Member ID input box.",
                "action": "type_text",
                "target_description": "Member ID input box",
                "selector_hint": "input[type='text']",
                "value": "1042",
            },
            # Decision for Step 3: click lookup
            {
                "thought": "I see the Lookup Member button. Clicking it to execute search.",
                "action": "click",
                "target_description": "Lookup Member button",
                "selector_hint": "input[type='submit']",
                "expected_feedback": "Regular Savings",
                "checkpoint_rule": {
                    "warning_pattern": "Warning: No member found",
                    "outcome_code": "MEMBER_NOT_FOUND",
                },
            },
            # Decision for Step 4: extract data
            {
                "thought": "Member details appeared. Extracting regular savings balance.",
                "action": "extract_data",
                "target_description": "Savings balance cell",
                "extraction_field": "savings_balance",
                "transform": "parse_currency",
            },
            # Decision for Step 5: finish
            {
                "thought": "Goal satisfied. Concluding discovery session.",
                "action": "finish",
            },
        ]

    def generate_decision(
        self,
        system_instruction: str,
        user_prompt: str,
        screenshot_png_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        if self.step < len(self.decisions):
            d = self.decisions[self.step]
            self.step += 1
            return d
        return {"action": "finish", "thought": "Completed"}


@pytest.fixture(scope="module")
def live_discovery_server():
    """Start mock bank server for discovery tests."""
    config = uvicorn.Config(app, host="127.0.0.1", port=DISCOVERY_PORT, log_level="error", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(0.6)
    yield DISCOVERY_SERVER_URL
    server.should_exit = True


@pytest.mark.asyncio
async def test_discovery_agent_and_compilation_pipeline(live_discovery_server: str, tmp_path: Path) -> None:
    """Verify DiscoveryAgent drives browser, records trace, and compiles valid CapabilityArtifact."""
    allowlist = RouteAllowlist([f"127.0.0.1:{DISCOVERY_PORT}", f"localhost:{DISCOVERY_PORT}"])
    stub_client = StubGeminiClient()

    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        agent = DiscoveryAgent(driver=driver, gemini_client=stub_client, evidence_dir=tmp_path)
        artifact = await agent.discover_capability(
            capability_id="lookup_savings_balance",
            goal="Lookup member 1042 and extract regular savings balance",
            start_url=f"{live_discovery_server}/servicing/lookup",
            target_app="HeritageCoreBanking",
            known_inputs={"member_id": "1042"},
            max_steps=6,
        )

        # Validate Compiled Artifact
        assert isinstance(artifact, CapabilityArtifact)
        assert artifact.metadata.capability_id == "lookup_savings_balance"
        assert len(artifact.steps) == 4  # nav, type, click, extract
        assert artifact.steps[1].value_binding == "$input.member_id"
        assert "savings_balance" in artifact.interface.outputs

        # Validate Evidence Persisted
        assert (tmp_path / "capability_artifact.json").exists()
        assert (tmp_path / "discovery_run.log").exists()
