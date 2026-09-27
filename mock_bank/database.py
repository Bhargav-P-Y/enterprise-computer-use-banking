"""In-memory mock banking database for simulating core banking records."""

import copy
import math
import threading
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Account:
    account_number: str
    account_type: str  # "Regular Savings", "Checking", "Money Market"
    balance: float
    available_balance: float
    status: str  # "ACTIVE", "FROZEN", "CLOSED"

    def __repr__(self) -> str:
        return f"Account(account_number='{self.account_number}', type='{self.account_type}', balance={self.balance}, status='{self.status}')"


@dataclass
class Member:
    member_id: str
    first_name: str
    last_name: str
    dob: str
    ssn_last4: str
    address: str
    phone: str
    accounts: List[Account] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def get_savings_balance(self) -> Optional[float]:
        for acc in self.accounts:
            if "Savings" in acc.account_type:
                return acc.balance
        return None

    def __repr__(self) -> str:
        return (
            f"Member(member_id='{self.member_id}', name='{self.full_name}', "
            f"dob='[REDACTED]', ssn='***-**-{self.ssn_last4}', accounts={len(self.accounts)})"
        )


def create_seed_members() -> Dict[str, Member]:
    """Factory function returning fresh canonical seed members."""
    return {
        "1042": Member(
            member_id="1042",
            first_name="Jane",
            last_name="Doe",
            dob="1984-06-12",
            ssn_last4="4412",
            address="742 Evergreen Terrace, Springfield, OR",
            phone="(555) 382-9102",
            accounts=[
                Account(
                    account_number="SAV-1042-01",
                    account_type="Regular Savings",
                    balance=4850.25,
                    available_balance=4850.25,
                    status="ACTIVE",
                ),
                Account(
                    account_number="CHK-1042-02",
                    account_type="Interest Checking",
                    balance=1240.80,
                    available_balance=1200.00,
                    status="ACTIVE",
                ),
            ],
        ),
        "2088": Member(
            member_id="2088",
            first_name="Robert",
            last_name="Smith",
            dob="1975-11-23",
            ssn_last4="8819",
            address="1204 Elm Street, Dallas, TX",
            phone="(555) 912-3844",
            accounts=[
                Account(
                    account_number="SAV-2088-01",
                    account_type="Regular Savings",
                    balance=12450.00,
                    available_balance=12450.00,
                    status="ACTIVE",
                ),
                Account(
                    account_number="CHK-2088-02",
                    account_type="Standard Checking",
                    balance=310.50,
                    available_balance=310.50,
                    status="ACTIVE",
                ),
            ],
        ),
        "3011": Member(
            member_id="3011",
            first_name="Maria",
            last_name="Garcia",
            dob="1991-03-08",
            ssn_last4="1092",
            address="450 Ocean Ave, Miami, FL",
            phone="(555) 781-4491",
            accounts=[
                Account(
                    account_number="SAV-3011-01",
                    account_type="Regular Savings",
                    balance=150.00,
                    available_balance=150.00,
                    status="FROZEN",
                )
            ],
        ),
    }


class BankDatabase:
    """Thread-safe in-memory bank database with pre-seeded test members."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._members: Dict[str, Member] = {}
        self.reset()

    @property
    def member_count(self) -> int:
        """Return total active member count."""
        with self._lock:
            return len(self._members)

    def __len__(self) -> int:
        with self._lock:
            return len(self._members)

    def reset(self) -> None:
        """Reset database to canonical test state."""
        with self._lock:
            self._members = create_seed_members()

    def get_member(self, member_id: str) -> Optional[Member]:
        """Fetch member by exact member ID with lock isolation."""
        with self._lock:
            member = self._members.get(str(member_id).strip())
            return copy.deepcopy(member) if member else None

    def search_members(self, query: str) -> List[Member]:
        """Search members by ID, first name, or last name with deepcopy isolation."""
        q = str(query).strip().lower()
        with self._lock:
            if not q:
                return [copy.deepcopy(m) for m in self._members.values()]

            results = []
            for m in self._members.values():
                if (
                    q == m.member_id.lower()
                    or q in m.first_name.lower()
                    or q in m.last_name.lower()
                    or q in m.full_name.lower()
                ):
                    results.append(m)
            return [copy.deepcopy(m) for m in results]

    def execute_transfer(
        self,
        from_member_id: str,
        to_account: str,
        amount: float,
        override_code: Optional[str] = None,
    ) -> Dict[str, str]:
        """Execute fund transfer with validation and manager override check."""
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or math.isnan(amount) or math.isinf(amount) or amount <= 0:
            return {"status": "ERROR", "message": "Transfer amount must be a positive finite number"}

        to_account_clean = str(to_account or "").strip()
        if not to_account_clean:
            return {"status": "ERROR", "message": "Destination account must be specified"}

        with self._lock:
            member = self._members.get(str(from_member_id).strip())
            if not member:
                return {"status": "ERROR", "message": "Member not found"}

            savings = None
            for acc in member.accounts:
                if "Savings" in acc.account_type:
                    savings = acc
                    break

            if not savings:
                return {"status": "ERROR", "message": "No savings account found for member"}

            if savings.status != "ACTIVE":
                return {"status": "ERROR", "message": f"Account is {savings.status}; cannot initiate transfer"}

            if amount > 5000.0 and override_code != "MGR-9941":
                return {
                    "status": "AUTH_REQUIRED",
                    "message": "Manager Override Code Required for transfers over $5,000",
                }

            if savings.available_balance < amount:
                return {"status": "ERROR", "message": "Insufficient available funds"}

            savings.balance -= amount
            savings.available_balance -= amount

            # Record internal double-entry credit if destination account exists in core records
            for m in self._members.values():
                for acc in m.accounts:
                    if acc.account_number == to_account_clean:
                        acc.balance += amount
                        acc.available_balance += amount
                        break

            conf_num = f"CONF-{uuid.uuid4().hex[:8].upper()}"
            return {
                "status": "SUCCESS",
                "confirmation_number": conf_num,
                "new_balance": f"${savings.balance:,.2f}",
            }


# Global singleton instance
db = BankDatabase()

