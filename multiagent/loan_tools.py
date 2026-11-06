"""Loan tools: look up loans by phone, get EMI details, and create a loan application.

Data lives in a JSON file (`LOANS_FILE`, default `loans.json`). If the file does not
exist it is created with a few sample loans so the lookup tools work out of the box.
Replace `LoanStore` with calls to your real loan system when you have one.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from .tools import ToolError, tool

LoanType = Literal["personal", "home", "car", "education", "business", "gold"]
EmploymentType = Literal["salaried", "self_employed", "business_owner", "student", "retired", "unemployed"]

# Annual interest rate (%) offered per loan type
INTEREST_RATES: dict[str, float] = {
    "personal": 13.5, "home": 8.75, "car": 9.5, "education": 10.5, "business": 15.0, "gold": 9.0,
}


# --- maths -----------------------------------------------------------------

def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    days_in_month = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, days_in_month))


def calculate_emi(principal: float, annual_rate: float, tenure_months: int) -> float:
    r = annual_rate / 12 / 100
    if r == 0:
        return round(principal / tenure_months, 2)
    f = (1 + r) ** tenure_months
    return round(principal * r * f / (f - 1), 2)


def _outstanding(principal: float, annual_rate: float, emi: float, paid: int) -> float:
    r = annual_rate / 12 / 100
    if r == 0:
        return max(0.0, round(principal - emi * paid, 2))
    f = (1 + r) ** paid
    return max(0.0, round(principal * f - emi * (f - 1) / r, 2))


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    digits = digits.lstrip("0") if len(digits) == 11 else digits
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise ToolError(f"'{phone}' is not a valid 10-digit Indian mobile number")
    return digits


# --- storage ---------------------------------------------------------------

def _sample_loans() -> list[dict[str, Any]]:
    def loan(loan_id, name, phone, loan_type, principal, tenure, first_emi, paid, status="active"):
        rate = INTEREST_RATES[loan_type]
        return {
            "loan_id": loan_id, "customer_name": name, "phone": phone, "loan_type": loan_type,
            "principal": principal, "annual_interest_rate": rate, "tenure_months": tenure,
            "emi_amount": calculate_emi(principal, rate, tenure), "first_emi_date": first_emi,
            "emis_paid": paid, "status": status, "created_at": "2025-01-01T00:00:00Z",
        }

    return [
        loan("LN100001", "Rahul Sharma", "9876543210", "personal", 300000, 36, "2025-02-05", 21),
        loan("LN100002", "Rahul Sharma", "9876543210", "car", 650000, 60, "2024-08-10", 27),
        loan("LN100003", "Priya Nair", "9123456780", "home", 4500000, 240, "2023-04-01", 43),
        loan("LN100004", "Amit Verma", "9988776655", "education", 800000, 84, "2022-07-15", 84, "closed"),
    ]


class LoanStore:
    """Thread-safe list of loans mirrored to a JSON file (atomic writes)."""

    def __init__(self, path: str | Path, seed: bool = True) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        if not self.path.exists():
            self._write(_sample_loans() if seed else [])

    def _read(self) -> list[dict[str, Any]]:
        return json.loads(self.path.read_text() or "[]")

    def _write(self, loans: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".loans-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(loans, f, indent=2)
        os.replace(tmp, self.path)

    def by_phone(self, phone: str) -> list[dict[str, Any]]:
        with self._lock:
            return [l for l in self._read() if l["phone"] == phone]

    def get(self, loan_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((l for l in self._read() if l["loan_id"] == loan_id), None)

    def add(self, loan: dict[str, Any]) -> None:
        with self._lock:
            loans = self._read()
            loans.append(loan)
            self._write(loans)


store = LoanStore(os.getenv("LOANS_FILE", "loans.json"))


# --- tools -----------------------------------------------------------------

def _summary(l: dict[str, Any]) -> dict[str, Any]:
    return {k: l[k] for k in ("loan_id", "loan_type", "principal", "emi_amount", "tenure_months", "status")}


@tool
def get_loans_by_phone(
    phone: Annotated[str, Field(description="Customer's registered 10-digit mobile number")],
) -> dict[str, Any]:
    """Find all loans registered to a customer's mobile number. Returns the customer name and a
    summary of each loan (loan_id, type, principal, EMI, tenure, status)."""
    p = _normalize_phone(phone)
    loans = store.by_phone(p)
    if not loans:
        return {"phone": p, "found": False, "message": "No loans found for this mobile number"}
    return {"phone": p, "found": True, "customer_name": loans[0]["customer_name"],
            "loans": [_summary(l) for l in loans]}


@tool
def get_emi_details(
    loan_id: Annotated[str, Field(description="Loan ID, e.g. LN100001")],
) -> dict[str, Any]:
    """Get EMI details for a loan: EMI amount, interest rate, EMIs paid and remaining, next due
    date, outstanding principal, and total interest and amount payable."""
    l = store.get(loan_id.strip().upper())
    if l is None:
        raise ToolError(f"No loan found with ID '{loan_id}'")
    total, paid, emi = l["tenure_months"], l["emis_paid"], l["emi_amount"]
    out: dict[str, Any] = {
        "loan_id": l["loan_id"], "customer_name": l["customer_name"], "loan_type": l["loan_type"],
        "status": l["status"], "principal": l["principal"], "annual_interest_rate": l["annual_interest_rate"],
        "emi_amount": emi, "tenure_months": total, "emis_paid": paid, "emis_remaining": total - paid,
        "total_interest": round(emi * total - l["principal"], 2), "total_payable": round(emi * total, 2),
        "outstanding_principal": _outstanding(l["principal"], l["annual_interest_rate"], emi, paid),
        "next_emi_date": None,
    }
    if l["status"] == "active" and l.get("first_emi_date") and paid < total:
        out["next_emi_date"] = _add_months(date.fromisoformat(l["first_emi_date"]), paid).isoformat()
    return out


@tool
def create_loan(
    customer_name: Annotated[str, Field(min_length=2, max_length=100, description="Full name as on PAN")],
    phone: Annotated[str, Field(description="10-digit mobile number")],
    pan: Annotated[str, Field(description="PAN number, e.g. ABCDE1234F")],
    loan_type: Annotated[LoanType, Field(description="Type of loan")],
    loan_amount: Annotated[float, Field(gt=0, le=50_000_000, description="Requested amount in INR")],
    tenure_months: Annotated[int, Field(ge=6, le=360, description="Repayment tenure in months")],
    employment_type: Annotated[EmploymentType, Field(description="Customer's employment type")],
    monthly_income: Annotated[float, Field(ge=0, description="Net monthly income in INR")],
    customer_confirmed: Annotated[bool, Field(description="True only if the customer explicitly confirmed "
                                                        "all details and agreed to submit")],
) -> dict[str, Any]:
    """Create a new loan application. Call this ONLY after collecting every field from the customer,
    reading the details back to them, and getting explicit confirmation to submit. Returns the new
    loan_id, the interest rate, and the EMI. The loan starts in 'pending_approval' status."""
    if not customer_confirmed:
        raise ToolError("Customer has not confirmed. Read the details back and get explicit confirmation first.")
    p = _normalize_phone(phone)
    pan = pan.strip().upper()
    if not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", pan):
        raise ToolError(f"'{pan}' is not a valid PAN (format: ABCDE1234F)")
    if loan_type == "home" and tenure_months > 360 or loan_type != "home" and tenure_months > 84:
        raise ToolError(f"Maximum tenure for a {loan_type} loan is {360 if loan_type == 'home' else 84} months")

    rate = INTEREST_RATES[loan_type]
    emi = calculate_emi(loan_amount, rate, tenure_months)
    if monthly_income and emi > 0.6 * monthly_income:
        raise ToolError(f"EMI of {emi:.2f} exceeds 60% of monthly income ({monthly_income:.2f}). "
                        "Suggest a smaller amount or a longer tenure.")

    loan = {
        "loan_id": "LN" + uuid.uuid4().hex[:8].upper(), "customer_name": customer_name.strip(),
        "phone": p, "pan": pan, "loan_type": loan_type, "principal": round(loan_amount, 2),
        "annual_interest_rate": rate, "tenure_months": tenure_months, "emi_amount": emi,
        "employment_type": employment_type, "monthly_income": monthly_income,
        "first_emi_date": None, "emis_paid": 0, "status": "pending_approval",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    store.add(loan)
    return {"created": True, "loan_id": loan["loan_id"], "status": loan["status"], "loan_type": loan_type,
            "principal": loan["principal"], "annual_interest_rate": rate, "tenure_months": tenure_months,
            "emi_amount": emi}
