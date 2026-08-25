from __future__ import annotations

from typing import Annotated

from pydantic import Field


ACCOUNTS = {
    "acct-demo-a": {"balance": 1250.50, "currency": "USD", "spend": 430.25},
    "acct-demo-b": {"balance": 875.00, "currency": "USD", "spend": 210.00},
}


def get_balance(
    account_id: Annotated[str, Field(description="Synthetic account identifier.")],
) -> dict:
    """Return the authoritative balance for one synthetic account."""
    if not account_id:
        return {"ok": False, "error": {"code": "account_id_required"}}
    record = ACCOUNTS.get(account_id)
    if record is None:
        return {"ok": False, "error": {"code": "account_not_found"}}
    return {"ok": True, "account_id": account_id, **record}


def get_budget_summary(
    account_id: Annotated[str, Field(description="Synthetic account identifier.")],
) -> dict:
    """Return bounded synthetic budget data for one account."""
    record = ACCOUNTS.get(account_id)
    if record is None:
        return {"ok": False, "error": {"code": "account_not_found"}}
    return {
        "ok": True,
        "account_id": account_id,
        "monthly_limit": 1000.0,
        "spent": record["spend"],
        "currency": record["currency"],
    }


def list_monthly_items(
    account_id: Annotated[str, Field(description="Synthetic account identifier.")],
) -> dict:
    """Return a small synthetic monthly item list."""
    if account_id not in ACCOUNTS:
        return {"ok": False, "error": {"code": "account_not_found"}}
    return {
        "ok": True,
        "account_id": account_id,
        "items": [
            {"label": "Public transit", "amount": 45.0},
            {"label": "Groceries", "amount": 132.5},
            {"label": "Utilities", "amount": 88.0},
        ],
    }
