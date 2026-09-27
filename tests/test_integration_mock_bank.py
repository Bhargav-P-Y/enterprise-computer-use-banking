"""Level 2 Integration Tests: Mock Bank Web Application Endpoints and DOM Contracts."""

import pytest
from starlette.testclient import TestClient
from mock_bank.server import app
from mock_bank.database import db


@pytest.fixture(autouse=True)
def reset_bank_db() -> None:
    """Reset database before each test."""
    db.reset()


def test_frameset_view_endpoint() -> None:
    """Verify base frameset delivers top header and iframe container."""
    with TestClient(app) as client:
        response = client.get("/servicing/frameset")
        assert response.status_code == 200
        assert "HERITAGE FEDERAL CREDIT UNION" in response.text
        assert 'iframe id="app_main_frame"' in response.text


def test_member_lookup_happy_path() -> None:
    """Verify member search finds member 1042 and returns details link."""
    with TestClient(app) as client:
        response = client.get("/servicing/lookup?ctl00$Main$txtMemId_8842=1042")
        assert response.status_code == 200
        assert "Jane Doe" in response.text
        assert "4850.25" in response.text
        assert 'href="/servicing/member/1042"' in response.text


def test_member_lookup_business_outcome_not_found() -> None:
    """Verify missing member 9999 returns 200 OK with explicit business outcome warning."""
    with TestClient(app) as client:
        response = client.get("/servicing/lookup?ctl00$Main$txtMemId_8842=9999")
        # Must be 200 OK (valid business outcome, NOT 500 crash or unhandled error)
        assert response.status_code == 200
        assert "Warning: No member found matching ID 9999" in response.text
        assert "ctl00_Main_lblError" in response.text


def test_member_detail_view() -> None:
    """Verify member detail view renders account ledger."""
    with TestClient(app) as client:
        response = client.get("/servicing/member/1042")
        assert response.status_code == 200
        assert "MEMBER RECORD: 1042" in response.text
        assert "Regular Savings" in response.text
        assert "$4,850.25" in response.text


def test_simulated_fault_500() -> None:
    """Verify simulated fault endpoint returns 500 status code for hard failure tests."""
    with TestClient(app) as client:
        response = client.get("/servicing/fault/500")
        assert response.status_code == 500
        assert "Internal System Exception" in response.text
