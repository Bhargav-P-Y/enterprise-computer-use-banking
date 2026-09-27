"""Unified Command Line Interface for Interface.ai Computer-Use Automation System.

Provides commands for mock server serving, autonomous LLM discovery,
zero-LLM deterministic replay, in-situ HITL live handoff, and LLM-as-a-judge audits.
"""

import asyncio
import json
from pathlib import Path
import urllib.parse
import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from src.schemas.artifact import CapabilityArtifact
from src.schemas.execution import ReplayInput, ReplayStatus
from src.surface.driver import SurfaceDriver
from src.guardrails.allowlist import RouteAllowlist
from src.guardrails.pii_redactor import PIIRedactor
from src.replay.executor import ReplayExecutor
from src.hitl.handoff import InSituHandoffManager

app = typer.Typer(
    name="interface-ai",
    help="Computer-Use Automation System for US Core Banking Back-Office Software.",
    add_completion=False,
)
console = Console()


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Binding host for mock bank server"),
    port: int = typer.Option(8123, help="Port for mock bank server"),
) -> None:
    """Start the legacy mock bank ASGI core server."""
    console.print(f"[bold green]Starting Mock Core Banking Server on http://{host}:{port}...[/bold green]")
    from mock_bank.server import app as starlette_app
    uvicorn.run(starlette_app, host=host, port=port, log_level="info")


@app.command()
def discover(
    goal: str = typer.Option("Lookup member 1042 and extract regular savings balance", help="Natural language business goal"),
    start_url: str = typer.Option("http://127.0.0.1:8123/servicing/lookup", "--url", "--start-url", help="Entrypoint URL"),
    member_id: str = typer.Option("1042", help="Member ID for initial exploration"),
    max_steps: int = typer.Option(8, help="Maximum discovery loop steps"),
    headless: bool = typer.Option(True, help="Run browser headlessly"),
    evidence_dir: str = typer.Option("evidence", help="Evidence output directory"),
) -> None:
    """Run Stage 1 Multimodal Discovery Loop using Gemini 3.8 Flash."""
    # Lazy imports preserve the deterministic zero-LLM boundary for replay (INV-05)
    from src.discovery.agent import DiscoveryAgent
    from src.discovery.gemini_client import DiscoveryGeminiClient

    safe_goal = PIIRedactor.redact_text(str(goal))
    console.print(f"[bold cyan]Initiating Multimodal Discovery: '{safe_goal}'...[/bold cyan]")

    async def _run() -> None:
        allowed_origins = ["127.0.0.1:8123", "localhost:8123"]
        if start_url:
            try:
                p = urllib.parse.urlsplit(start_url)
                host = (p.hostname or "").lower()
                if host in ("127.0.0.1", "localhost", "::1", "testclient") or host.endswith(".bank.internal") or host.endswith(".corp.internal"):
                    if p.netloc:
                        allowed_origins.append(p.netloc)
            except Exception:
                pass
        allowlist = RouteAllowlist(allowed_origins)
        client = DiscoveryGeminiClient()

        async with SurfaceDriver(headless=headless, allowlist=allowlist) as driver:
            agent = DiscoveryAgent(
                driver=driver,
                gemini_client=client,
                evidence_dir=Path(evidence_dir),
            )
            artifact = await agent.discover_capability(
                capability_id="lookup_savings_balance",
                goal=goal,
                start_url=start_url,
                target_app="HeritageCoreBanking",
                known_inputs={"member_id": member_id},
                max_steps=max_steps,
            )
            console.print(f"[bold green]Discovery complete! Compiled {len(artifact.steps)} steps.[/bold green]")
            console.print(f"Artifact exported to: {evidence_dir}/capability_artifact.json")
            console.print(f"Trace log exported to: {evidence_dir}/discovery_run.log")

    asyncio.run(_run())


@app.command()
def replay(
    artifact_path: str = typer.Option("evidence/capability_artifact.json", "--artifact", "--artifact-path", help="Path to CapabilityArtifact JSON"),
    params: str = typer.Option('{"member_id":"1042"}', "--params", "--input", help="Input parameters JSON string"),
    headless: bool = typer.Option(True, help="Run browser headlessly"),
    evidence_dir: str = typer.Option("evidence", help="Evidence directory"),
) -> None:
    """Run Stage 3 Deterministic Zero-LLM Replay of a compiled capability artifact."""
    console.print(f"[bold magenta]Starting Deterministic Replay (Zero-LLM Firewall)...[/bold magenta]")

    art_path = Path(artifact_path)
    if not art_path.exists():
        console.print(f"[bold red]Error: Artifact not found at '{artifact_path}'. Run 'discover' first.[/bold red]")
        raise typer.Exit(1)

    try:
        raw_json = art_path.read_text(encoding="utf-8")
        artifact = CapabilityArtifact.model_validate_json(raw_json)
    except Exception as e:
        clean_err = PIIRedactor.redact_text(str(e))
        console.print(f"[bold red]Error: Invalid CapabilityArtifact schema in '{artifact_path}': {clean_err}[/bold red]")
        raise typer.BadParameter(f"Invalid artifact: {clean_err}")

    try:
        parsed_params = json.loads(params)
        if not isinstance(parsed_params, dict):
            raise ValueError("Parameters must be a JSON object")
    except Exception as e:
        console.print(f"[bold red]Error: Invalid JSON parameters '{PIIRedactor.redact_text(params)}': {e}[/bold red]")
        raise typer.BadParameter(f"Invalid JSON parameters: {e}")

    replay_input = ReplayInput(capability_id=artifact.metadata.capability_id, parameters=parsed_params)

    async def _run() -> ReplayStatus:
        allowed_origins = ["127.0.0.1:8123", "localhost:8123"]
        if hasattr(artifact, "metadata") and artifact.metadata.start_url:
            try:
                p = urllib.parse.urlsplit(artifact.metadata.start_url)
                host = (p.hostname or "").lower()
                if host in ("127.0.0.1", "localhost", "::1", "testclient") or host.endswith(".bank.internal") or host.endswith(".corp.internal"):
                    if p.netloc:
                        allowed_origins.append(p.netloc)
            except Exception:
                pass
        allowlist = RouteAllowlist(allowed_origins)
        async with SurfaceDriver(headless=headless, allowlist=allowlist) as driver:
            executor = ReplayExecutor(driver=driver, evidence_dir=Path(evidence_dir))
            result = await executor.execute(artifact, replay_input)

            # Display formatted output with strict PII redaction (INV-28)
            color = "green" if result.status == ReplayStatus.SUCCESS else "yellow" if result.status == ReplayStatus.BUSINESS_OUTCOME else "red"
            console.print(f"[{color}]Status: {result.status.value.upper()}[/{color}]")
            console.print(f"Duration: {result.duration_ms} ms")
            console.print(f"Message: {PIIRedactor.redact_text(result.message)}")
            if result.data:
                clean_data = PIIRedactor.redact_object(result.data)
                console.print("Data Extracted:", json.dumps(clean_data, indent=2, default=str))
            if result.failure_context:
                clean_context = PIIRedactor.redact_object(result.failure_context)
                console.print("Failure Context:", json.dumps(clean_context, indent=2, default=str))
            return result.status

    try:
        final_status = asyncio.run(_run())
        if final_status == ReplayStatus.HARD_FAILURE:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as err:
        clean_err = PIIRedactor.redact_text(str(err))
        console.print(f"[bold red]Replay execution failure: {clean_err}[/bold red]")
        raise typer.Exit(code=1)


@app.command("hitl-demo")
def hitl_demo(
    url: str = typer.Option("http://127.0.0.1:8123/servicing/lookup", help="Target URL"),
    headless: bool = typer.Option(True, help="Run browser headlessly"),
    evidence_dir: str = typer.Option("evidence", help="Evidence output directory"),
) -> None:
    """Demonstrate In-Situ Live Session HITL Handoff Seam with Dual-Stream Recording."""
    console.print("[bold yellow]Initiating In-Situ Human-in-the-Loop Handoff Demo...[/bold yellow]")

    async def _run() -> None:
        allowed_origins = ["127.0.0.1:8123", "localhost:8123"]
        if url:
            try:
                p = urllib.parse.urlsplit(url)
                host = (p.hostname or "").lower()
                if host in ("127.0.0.1", "localhost", "::1", "testclient") or host.endswith(".bank.internal") or host.endswith(".corp.internal"):
                    if p.netloc:
                        allowed_origins.append(p.netloc)
            except Exception:
                pass
        allowlist = RouteAllowlist(allowed_origins)
        async with SurfaceDriver(headless=headless, allowlist=allowlist) as driver:
            await driver.navigate(url)
            mgr = InSituHandoffManager(driver=driver, evidence_dir=Path(evidence_dir))

            # Simulate handoff trigger
            handoff_task = asyncio.create_task(
                mgr.initiate_handoff(
                    capability_id="hitl_live_demo",
                    trigger_reason="Compliance override confirmation required",
                    timeout_seconds=5,
                )
            )

            try:
                # Interact via SurfaceDriver semantic primitives adhering to Law of Demeter (INV-03)
                try:
                    await driver.wait_for_selector("input[type='text']", timeout_ms=3000)
                    await driver.fill(value="2088", selector="input[type='text']", timeout_ms=2000)
                except Exception as e:
                    console.print(f"[yellow]HITL demo input note: {e}[/yellow]")

                try:
                    await driver.wait_for_selector("#__interface_ai_resume_btn__", timeout_ms=3000)
                    await driver.click(selector="#__interface_ai_resume_btn__", timeout_ms=2000)
                except Exception as e:
                    console.print(f"[yellow]HITL demo resume note: {e}[/yellow]")

                record = await handoff_task
                console.print(f"[bold green]HITL Intervention completed successfully![/bold green]")
                console.print(f"Actions Recorded: {len(record.recorded_actions)}")
                console.print(f"State Delta Summary: {record.state_delta.summary if record.state_delta else 'N/A'}")
                console.print(f"Evidence exported to: {evidence_dir}/human_intervention.json")
            finally:
                if not handoff_task.done():
                    handoff_task.cancel()

    asyncio.run(_run())


@app.command()
def audit() -> None:
    """Run Gemini 3.8 REST LLM-as-a-Judge Audit on Repository Code."""
    console.print("[bold cyan]=== RUNNING GEMINI 3.8 REST LLM-AS-A-JUDGE CODE QUALITY AUDITOR ===[/bold cyan]\n")
    import importlib.util
    judge_path = (Path(__file__).resolve().parent.parent / "code" / "evaluation" / "code_judge.py").resolve()
    if not judge_path.exists():
        fallback = Path.cwd() / "code" / "evaluation" / "code_judge.py"
        if fallback.exists():
            judge_path = fallback
    spec = importlib.util.spec_from_file_location("code_judge", judge_path)
    if not spec or not spec.loader:
        console.print("[bold red]Failed to load code_judge module[/bold red]")
        raise typer.Exit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    judge = mod.CodeJudge()

    target_files = [
        # Phase 1: Mock Core Banking System
        Path("mock_bank/database.py"),
        Path("mock_bank/server.py"),
        # Phase 2: Schemas & Guardrails Citadel
        Path("src/schemas/artifact.py"),
        Path("src/schemas/execution.py"),
        Path("src/schemas/hitl.py"),
        Path("src/guardrails/allowlist.py"),
        Path("src/guardrails/risk_governor.py"),
        Path("src/guardrails/pii_redactor.py"),
        # Phase 3: Surface Driver & Multi-Tier Locators
        Path("src/surface/driver.py"),
        Path("src/surface/locator_engine.py"),
        Path("src/surface/som_annotator.py"),
        # Phase 4: Zero-LLM Deterministic Replay Engine
        Path("src/replay/classifier.py"),
        Path("src/replay/recovery.py"),
        Path("src/replay/executor.py"),
        # Phase 5: In-Situ Live Session HITL Seam
        Path("src/hitl/diff_engine.py"),
        Path("src/hitl/handoff.py"),
        # Phase 6: Multimodal Discovery & Capability Compiler
        Path("src/discovery/gemini_client.py"),
        Path("src/discovery/prompts.py"),
        Path("src/discovery/compiler.py"),
        Path("src/discovery/agent.py"),
        # Phase 7: Unified CLI & System Entrypoint
        Path("src/cli.py"),
    ]
    report = judge.audit_phase(target_files)

    table = Table(title="LLM-as-a-Judge Code Quality Audit Results")
    table.add_column("File", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Score", justify="right")
    table.add_column("Model Used", style="dim")
    table.add_column("Assessment", style="italic")

    for r in report.reports:
        st = r.status
        color = "green" if st == "PASS" else "red"
        table.add_row(
            r.file,
            f"[{color}]{st}[/{color}]",
            f"{r.score}/100",
            r.model_used,
            r.summary[:70] + "...",
        )

    console.print(table)
    console.print(f"\n[bold]Total Files Audited:[/bold] {report.total_files} | [bold]Pass Rate:[/bold] {report.pass_rate} | [bold]Average Score:[/bold] {report.average_score}/100\n")



if __name__ == "__main__":
    app()
