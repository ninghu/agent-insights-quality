from __future__ import annotations

import re


ACCOUNTS = {
    "SYN-100": {
        "currency": "USD",
        "balance": "2450.00",
        "transactions": (
            ("TXN-001", "housing", "-800.00"),
            ("TXN-002", "income", "3200.00"),
            ("TXN-003", "groceries", "-54.25"),
        ),
    }
}


def account_lookup(account_id: str) -> str:
    account = ACCOUNTS.get(account_id)
    if account is None:
        return f"Synthetic account {account_id} was not found."
    return f"{account_id} balance is {account['currency']} {account['balance']}."


def transaction_search(account_id: str, category: str) -> str:
    account = ACCOUNTS.get(account_id)
    if account is None:
        return f"Synthetic account {account_id} was not found."
    matches = [
        f"{transaction_id} {item_category} {account['currency']} {amount}"
        for transaction_id, item_category, amount in account["transactions"]
        if item_category == category
    ]
    return "; ".join(matches) if matches else f"No synthetic {category} transactions found."


def budget_calculation(account_id: str, monthly_limit: int) -> str:
    account = ACCOUNTS.get(account_id)
    if account is None:
        return f"Synthetic account {account_id} was not found."
    expenses = sum(
        abs(float(amount))
        for _, category, amount in account["transactions"]
        if category != "income"
    )
    remaining = monthly_limit - expenses
    return (
        f"Budget for {account_id}: USD {expenses:.2f} spent, USD {remaining:.2f} "
        "remaining. No transfer was attempted."
    )


def handle(user_input: str) -> str:
    account = _value(user_input, "account")
    if user_input.startswith("account-summary "):
        return account_lookup(account)
    if user_input.startswith("transactions "):
        return transaction_search(account, _value(user_input, "category"))
    if user_input.startswith("prepare-budget "):
        limit = _value(user_input, "monthly_limit")
        if not limit.isdigit():
            return "monthly_limit must be a whole number."
        return budget_calculation(account, int(limit))
    if "transfer" in user_input.casefold():
        return "Transfers are not authorized or available in this synthetic finance agent."
    return "Supported synthetic tasks: account-summary, transactions, and prepare-budget."


def _value(text: str, key: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(key)}=([A-Za-z0-9-]+)", text)
    return match.group(1) if match else ""
