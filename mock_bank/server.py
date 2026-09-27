"""Starlette ASGI web server hosting the mock legacy core banking application."""

import asyncio
import ipaddress
import math
import os
import time
from pathlib import Path
from typing import Any
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from mock_bank.database import db

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def format_currency(value: Any) -> str:
    """Format float as currency with commas and defensive fallback."""
    try:
        val = float(value)
        return f"{val:,.2f}" if math.isfinite(val) else "0.00"
    except (ValueError, TypeError, OverflowError):
        return "0.00"

templates.env.filters["currency"] = format_currency


async def root_redirect(request: Request) -> RedirectResponse:
    """Redirect root to legacy frameset entrypoint."""
    return RedirectResponse(url="/servicing/frameset")


async def frameset_view(request: Request) -> HTMLResponse:
    """Render the main legacy frameset container."""
    return templates.TemplateResponse(request, "base_frameset.html")


async def member_search_view(request: Request) -> HTMLResponse:
    """Render member search console and process lookup queries."""
    query = (
        request.query_params.get("ctl00$Main$txtMemId_8842")
        or request.query_params.get("member_id")
        or request.query_params.get("q")
        or ""
    ).strip()[:64]

    members = []
    error_message = None

    if query:
        member = db.get_member(query)
        if member:
            members = [member]
        else:
            # Legitimate business outcome: 200 OK with explicit not found message
            error_message = f"Warning: No member found matching ID {query}"

    return templates.TemplateResponse(
        request,
        "member_search.html",
        {
            "query": query,
            "members": members,
            "error_message": error_message,
        },
    )


async def member_detail_view(request: Request) -> HTMLResponse:
    """Render individual member details with account share ledger."""
    member_id = request.path_params.get("member_id", "").strip()
    simulate_delay = request.query_params.get("simulate_delay") == "1"
    if simulate_delay:
        await asyncio.sleep(0.35)

    member = db.get_member(member_id)
    if not member:
        return templates.TemplateResponse(
            request,
            "error_pages.html",
            {
                "error_title": "Member Record Not Located",
                "error_details": f"The requested account record ID '{member_id}' does not exist.",
                "timestamp": str(int(time.time())),
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        request,
        "member_detail.html",
        {
            "member": member,
            "simulate_delay": simulate_delay,
        },
    )


async def transfer_wizard_view(request: Request) -> HTMLResponse:
    """Render fund transfer wizard and process submissions."""
    if request.method == "POST":
        form = await request.form()
        from_member = str(form.get("ctl00$Main$txtFromMember") or form.get("from_member") or "").strip()[:32]
        to_account = str(form.get("ctl00$Main$txtToAccount") or "SAV-9910-01").strip()[:32]
        amount_str = str(form.get("ctl00$Main$txtAmount") or "0").strip()[:16]
        override_code = str(form.get("override_code") or "").strip()[:16]

        try:
            amount = float(amount_str)
            if not math.isfinite(amount) or amount <= 0:
                amount = 0.0
        except (ValueError, OverflowError):
            amount = 0.0

        # Check transfer rules
        result = db.execute_transfer(from_member, to_account, amount, override_code=override_code)

        if result.get("status") == "AUTH_REQUIRED":
            return templates.TemplateResponse(
                request,
                "transfer_wizard.html",
                {
                    "from_member": from_member,
                    "auth_required": True,
                    "amount": amount,
                },
            )

        if result.get("status") == "SUCCESS":
            return templates.TemplateResponse(
                request,
                "transfer_wizard.html",
                {
                    "from_member": from_member,
                    "success_data": result,
                },
            )

        # General error - explicit 400 for validation rejection
        return templates.TemplateResponse(
            request,
            "error_pages.html",
            {
                "error_title": "Transfer Execution Rejected",
                "error_details": result.get("message", "Validation failure"),
                "timestamp": str(int(time.time())),
            },
            status_code=400,
        )

    # GET request
    from_member = request.query_params.get("from_member", "")
    return templates.TemplateResponse(
        request,
        "transfer_wizard.html",
        {
            "from_member": from_member,
            "auth_required": False,
        },
    )


async def system_status_view(request: Request) -> HTMLResponse:
    """Render system status and diagnostics."""
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;padding:20px;'>"
        "<h2>Core Banking Host System Status</h2>"
        "<p style='color:green;'>&#9679; ALL HOST INTERFACES ONLINE</p>"
        "<p>Active Tenants: 420 | Database Pool: HEALTHY | Uptime: 99.98%</p>"
        "<a href='/servicing/lookup'>&larr; Back to Member Search</a>"
        "</body></html>"
    )


async def simulated_fault_view(request: Request) -> HTMLResponse:
    """Simulate an internal unhandled 500 server crash for testing hard failure recovery."""
    return templates.TemplateResponse(
        request,
        "error_pages.html",
        {
            "error_title": "Internal System Exception (HTTP 500)",
            "error_details": "Fatal database deadlock detected at HostConnector.ExecuteTransaction().",
            "timestamp": str(int(time.time())),
        },
        status_code=500,
    )


async def reset_database_api(request: Request) -> JSONResponse:
    """API endpoint to reset database state for clean test iterations."""
    client_host = request.client.host if request.client else "unknown"
    is_loopback = client_host in ("127.0.0.1", "::1", "localhost", "testclient", "::ffff:127.0.0.1")
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        return JSONResponse({"error": "Forbidden: Local loopback only"}, status_code=403)
    db.reset()
    return JSONResponse({"status": "RESET_OK", "members_count": db.member_count})


routes = [
    Route("/", root_redirect, methods=["GET"]),
    Route("/servicing/frameset", frameset_view, methods=["GET"]),
    Route("/servicing/lookup", member_search_view, methods=["GET"]),
    Route("/servicing/member/{member_id}", member_detail_view, methods=["GET"]),
    Route("/servicing/transfer", transfer_wizard_view, methods=["GET", "POST"]),
    Route("/servicing/status", system_status_view, methods=["GET"]),
    Route("/servicing/fault/500", simulated_fault_view, methods=["GET"]),
    Route("/api/reset", reset_database_api, methods=["POST"]),
]

app = Starlette(debug=os.getenv("BANK_DEBUG", "false").lower() == "true", routes=routes)


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Entrypoint to launch the server with uvicorn."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_server()
