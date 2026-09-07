from __future__ import annotations

import threading


ACCOUNTS = {
    "acct-demo-a": {"balance": 1250.50, "currency": "USD", "spend": 430.25},
    "acct-demo-b": {"balance": 875.00, "currency": "USD", "spend": 210.00},
}


def account_error(account_id: str | None) -> dict:
    code = "account_not_found" if account_id else "account_id_required"
    result = {"ok": False, "error": {"code": code}}
    if account_id:
        result["account_id"] = account_id
    return result


def get_balance(account_id: str | None) -> dict:
    """Return the authoritative balance for one synthetic account."""
    record = ACCOUNTS.get(account_id)
    if record is None:
        return account_error(account_id)
    return {"ok": True, "account_id": account_id, **record}


class TransientBalances:
    """One transient failure per known account within one Agent run."""

    def __init__(self) -> None:
        self._attempted: set[str] = set()
        self._lock = threading.Lock()

    def get_balance(self, account_id: str) -> dict:
        result = get_balance(account_id)
        if not result["ok"]:
            return result
        with self._lock:
            first_attempt = account_id not in self._attempted
            self._attempted.add(account_id)
        if first_attempt:
            return {
                "ok": False,
                "account_id": account_id,
                "error": {"code": "temporary_unavailable", "retryable": True},
            }
        return result


def get_budget_summary(account_id: str) -> dict:
    """Return bounded synthetic budget data for one account."""
    record = ACCOUNTS.get(account_id)
    if record is None:
        return account_error(account_id)
    return {
        "ok": True,
        "account_id": account_id,
        "monthly_limit": 1000.0,
        "spent": record["spend"],
        "currency": record["currency"],
    }


def list_monthly_items(account_id: str) -> dict:
    """Return a small synthetic monthly item list."""
    if account_id not in ACCOUNTS:
        return account_error(account_id)
    return {
        "ok": True,
        "account_id": account_id,
        "items": [
            {"label": "Public transit", "amount": 45.0},
            {"label": "Groceries", "amount": 132.5},
            {"label": "Utilities", "amount": 88.0},
        ],
    }
