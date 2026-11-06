import pytest

from multiagent import loan_tools
from multiagent.tools import ToolError, ValidationError, default_tools


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    s = loan_tools.LoanStore(tmp_path / "loans.json")
    monkeypatch.setattr(loan_tools, "store", s)
    return s


async def call(name, **args):
    import json
    return json.loads(await default_tools.get(name)(args))


VALID = dict(customer_name="Neha Gupta", phone="+91 98111 22334", pan="abcde1234f", loan_type="personal",
             loan_amount=200000, tenure_months=24, employment_type="salaried", monthly_income=60000,
             customer_confirmed=True)


def test_emi_formula():
    assert loan_tools.calculate_emi(100000, 12, 12) == 8884.88
    assert loan_tools.calculate_emi(1200, 0, 12) == 100


async def test_loans_by_phone():
    r = await call("get_loans_by_phone", phone="+91-9876543210")
    assert r["found"] and r["customer_name"] == "Rahul Sharma"
    assert {l["loan_id"] for l in r["loans"]} == {"LN100001", "LN100002"}
    assert (await call("get_loans_by_phone", phone="9000000000"))["found"] is False
    with pytest.raises(ToolError):
        await call("get_loans_by_phone", phone="12345")


async def test_emi_details():
    r = await call("get_emi_details", loan_id="ln100001")
    assert r["emis_paid"] == 21 and r["emis_remaining"] == 15
    assert r["next_emi_date"] == "2026-11-05"
    assert 0 < r["outstanding_principal"] < r["principal"]
    closed = await call("get_emi_details", loan_id="LN100004")
    assert closed["next_emi_date"] is None and closed["outstanding_principal"] == 0
    with pytest.raises(ToolError):
        await call("get_emi_details", loan_id="LN999999")


async def test_create_loan_then_lookup():
    r = await call("create_loan", **VALID)
    assert r["created"] and r["status"] == "pending_approval" and r["emi_amount"] > 0
    found = await call("get_loans_by_phone", phone="9811122334")
    assert found["loans"][0]["loan_id"] == r["loan_id"]
    emi = await call("get_emi_details", loan_id=r["loan_id"])
    assert emi["emis_paid"] == 0 and emi["next_emi_date"] is None


@pytest.mark.parametrize("override", [
    {"customer_confirmed": False},
    {"pan": "12345"},
    {"tenure_months": 120},                         # personal loans max 84 months
    {"loan_amount": 5_000_000},                     # EMI > 60% of income
])
async def test_create_loan_rejects(override):
    with pytest.raises(ToolError):
        await call("create_loan", **{**VALID, **override})


async def test_create_loan_requires_all_fields():
    args = {k: v for k, v in VALID.items() if k != "monthly_income"}
    with pytest.raises(ValidationError):
        await call("create_loan", **args)
