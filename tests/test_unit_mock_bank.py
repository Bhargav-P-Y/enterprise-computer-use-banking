"""Level 1 Unit Tests: Bank Database Logic and Data Integrity."""

from mock_bank.database import BankDatabase


def test_bank_database_initial_seed() -> None:
    """Verify default seeded bank members exist with correct balances."""
    db = BankDatabase()
    m1 = db.get_member("1042")
    assert m1 is not None
    assert m1.full_name == "Jane Doe"
    assert m1.get_savings_balance() == 4850.25
    assert m1.accounts[0].status == "ACTIVE"

    m2 = db.get_member("2088")
    assert m2 is not None
    assert m2.full_name == "Robert Smith"
    assert m2.get_savings_balance() == 12450.00


def test_bank_database_member_not_found() -> None:
    """Verify non-existent member returns None cleanly without error."""
    db = BankDatabase()
    assert db.get_member("9999") is None
    assert db.get_member("unknown_id") is None


def test_bank_database_search() -> None:
    """Verify member search matches ID, first name, and last name."""
    db = BankDatabase()
    assert len(db.search_members("1042")) == 1
    assert len(db.search_members("Jane")) == 1
    assert len(db.search_members("Smith")) == 1
    assert len(db.search_members("NonExistent")) == 0


def test_bank_database_transfer_standard() -> None:
    """Verify standard transfer under $5000 deducts from savings."""
    db = BankDatabase()
    res = db.execute_transfer(from_member_id="1042", to_account="SAV-9910-01", amount=500.0)
    assert res["status"] == "SUCCESS"
    assert "CONF-" in res["confirmation_number"]
    assert db.get_member("1042").get_savings_balance() == 4350.25


def test_bank_database_transfer_manager_override_policy() -> None:
    """Verify transfer over $5000 requires supervisor override code."""
    db = BankDatabase()
    # Attempt without code
    res1 = db.execute_transfer(from_member_id="2088", to_account="SAV-9910-01", amount=6000.0)
    assert res1["status"] == "AUTH_REQUIRED"

    # Attempt with valid override code
    res2 = db.execute_transfer(
        from_member_id="2088", to_account="SAV-9910-01", amount=6000.0, override_code="MGR-9941"
    )
    assert res2["status"] == "SUCCESS"
    assert db.get_member("2088").get_savings_balance() == 6450.00
