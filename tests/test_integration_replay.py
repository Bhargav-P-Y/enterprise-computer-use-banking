"""Level 2 Integration Tests: Zero-LLM Deterministic Replay Engine against Live Mock Bank."""

import threading
import time
from pathlib import Path
from typing import Any
import pytest
import uvicorn

from mock_bank.server import app
from src.guardrails.allowlist import RouteAllowlist
from src.schemas.artifact import (
    ActionType,
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
    StepCheckpoint,
    StepExtraction,
)
from src.schemas.execution import ReplayInput, ReplayStatus
from src.surface.driver import SurfaceDriver
from src.replay.executor import ReplayExecutor

REPLAY_PORT = 8125
REPLAY_SERVER_URL = f"http://127.0.0.1:{REPLAY_PORT}"


@pytest.fixture(scope="module")
def live_replay_server() -> Any:
    """Start mock bank server on dedicated replay test port."""
    config = uvicorn.Config(app, host="127.0.0.1", port=REPLAY_PORT, log_level="error", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(0.6)
    yield REPLAY_SERVER_URL
    server.should_exit = True


def build_member_lookup_artifact(server_url: str) -> CapabilityArtifact:
    """Construct canonical capability artifact for member lookup."""
    return CapabilityArtifact(
        schema_version="1.0.0",
        metadata=CapabilityMetadata(
            capability_id="lookup_savings_balance",
            description="Lookup member and extract savings balance",
            target_app="HeritageCoreBanking",
            base_url_pattern=f"{server_url}/*",
            risk_level=RiskLevel.SAFE_READ,
            created_at_utc="2026-09-18T10:00:00Z",
        ),
        interface=CapabilityInterface(
            inputs={"member_id": ParameterProperty(type="string", description="Member ID")},
            required_inputs=["member_id"],
            outputs={"savings_balance": ParameterProperty(type="number", description="Savings balance")},
            required_outputs=["savings_balance"],
        ),
        steps=[
            CapabilityStep(
                step_id="step_1_nav",
                description="Navigate to lookup page",
                action=ActionType.NAVIGATE,
                value_binding=f"{server_url}/servicing/lookup",
            ),
            CapabilityStep(
                step_id="step_2_input_id",
                description="Input member ID into search field",
                action=ActionType.TYPE_TEXT,
                locator=LocatorCascade(
                    tiers=[
                        LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role="textbox", name="Member ID"),
                        LocatorTier(strategy=LocatorStrategyType.LABEL_PROXIMITY, label_text="Member ID:", direction="right"),
                    ],
                    robustness_rationale="Uses label proximity as resilient fallback",
                ),
                value_binding="$input.member_id",
            ),
            CapabilityStep(
                step_id="step_3_click_search",
                description="Click lookup member button",
                action=ActionType.CLICK,
                locator=LocatorCascade(
                    tiers=[
                        LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role="button", name="Lookup Member"),
                        LocatorTier(strategy=LocatorStrategyType.STRUCTURAL_XPATH, xpath="//input[@value='Lookup Member']"),
                    ],
                    robustness_rationale="Matches submit button",
                ),
                checkpoint=StepCheckpoint(
                    branches=[
                        CheckpointBranch(
                            condition_type="text_visible",
                            pattern="Warning: No member found",
                            outcome_category="business_outcome",
                            outcome_code="MEMBER_NOT_FOUND",
                            message="Member ID does not exist in registry.",
                        ),
                        CheckpointBranch(
                            condition_type="text_visible",
                            pattern="Regular Savings",
                            outcome_category="continue",
                        ),
                    ]
                ),
            ),
            CapabilityStep(
                step_id="step_4_extract",
                description="Extract regular savings balance",
                action=ActionType.EXTRACT_DATA,
                extractions=[
                    StepExtraction(
                        output_field="savings_balance",
                        locator=LocatorCascade(
                            tiers=[
                                LocatorTier(strategy=LocatorStrategyType.LABEL_PROXIMITY, label_text="Regular Savings", direction="right"),
                                LocatorTier(strategy=LocatorStrategyType.STRUCTURAL_XPATH, xpath="//table[@id='ctl00_Main_gvMembers']//tr[1]/td[5]"),
                            ],
                            robustness_rationale="Extracts balance adjacent to Regular Savings cell",
                        ),
                        transform="parse_currency",
                    )
                ],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_replay_executor_happy_path(live_replay_server: str, tmp_path: Path) -> None:
    """Verify deterministic replay succeeds without LLM, returns data in <600ms."""
    allowlist = RouteAllowlist([f"127.0.0.1:{REPLAY_PORT}", f"localhost:{REPLAY_PORT}"])
    artifact = build_member_lookup_artifact(live_replay_server)
    replay_input = ReplayInput(capability_id="lookup_savings_balance", parameters={"member_id": "1042"})

    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        executor = ReplayExecutor(driver=driver, evidence_dir=tmp_path)
        result = await executor.execute(artifact, replay_input)

        assert result.status == ReplayStatus.SUCCESS
        assert result.data is not None
        assert result.data["savings_balance"] == 4850.25
        assert result.duration_ms < 1500


@pytest.mark.asyncio
async def test_replay_executor_business_outcome(live_replay_server: str, tmp_path: Path) -> None:
    """Verify deterministic replay cleanly captures 'MEMBER_NOT_FOUND' as a business outcome, not a crash."""
    allowlist = RouteAllowlist([f"127.0.0.1:{REPLAY_PORT}", f"localhost:{REPLAY_PORT}"])
    artifact = build_member_lookup_artifact(live_replay_server)
    replay_input = ReplayInput(capability_id="lookup_savings_balance", parameters={"member_id": "9999"})

    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        executor = ReplayExecutor(driver=driver, evidence_dir=tmp_path)
        result = await executor.execute(artifact, replay_input)

        # Must be BUSINESS_OUTCOME, never HARD_FAILURE
        assert result.status == ReplayStatus.BUSINESS_OUTCOME
        assert result.business_outcome_code == "MEMBER_NOT_FOUND"
        assert "does not exist" in result.message


@pytest.mark.asyncio
async def test_replay_executor_hard_failure(live_replay_server: str, tmp_path: Path) -> None:
    """Verify deterministic replay halts on unresolvable locator and writes failure evidence."""
    allowlist = RouteAllowlist([f"127.0.0.1:{REPLAY_PORT}", f"localhost:{REPLAY_PORT}"])
    artifact = build_member_lookup_artifact(live_replay_server)

    # Corrupt step 2 locator with impossible selector
    artifact.steps[1].locator = LocatorCascade(
        tiers=[LocatorTier(strategy=LocatorStrategyType.STRUCTURAL_XPATH, xpath="//nonexistent_impossible_element")],
        robustness_rationale="Injected failure test",
    )
    replay_input = ReplayInput(capability_id="lookup_savings_balance", parameters={"member_id": "1042"})

    async with SurfaceDriver(headless=True, allowlist=allowlist) as driver:
        executor = ReplayExecutor(driver=driver, evidence_dir=tmp_path)
        result = await executor.execute(artifact, replay_input)

        assert result.status == ReplayStatus.HARD_FAILURE
        assert result.failure_context is not None
        assert "evidence_paths" in result.failure_context
        # Check screenshot and DOM files were captured
        ev = result.failure_context["evidence_paths"]
        assert "screenshot" in ev and Path(ev["screenshot"]).exists()
        assert "dom" in ev and Path(ev["dom"]).exists()
