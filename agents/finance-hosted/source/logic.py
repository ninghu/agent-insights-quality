from __future__ import annotations

from typing import Any


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

TOOLS = [
    {
        "type": "function",
        "name": "account_lookup",
        "description": "Read one synthetic account balance.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["account_id"],
            "properties": {
                "account_id": {"type": "string", "enum": ["SYN-100"]},
            },
        },
    },
    {
        "type": "function",
        "name": "transaction_search",
        "description": "Search synthetic transactions by account and category.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["account_id", "category"],
            "properties": {
                "account_id": {"type": "string", "enum": ["SYN-100"]},
                "category": {
                    "type": "string",
                    "enum": ["groceries", "housing", "income"],
                },
            },
        },
    },
    {
        "type": "function",
        "name": "budget_calculation",
        "description": "Calculate a synthetic budget without moving funds.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["account_id", "monthly_limit"],
            "properties": {
                "account_id": {"type": "string", "enum": ["SYN-100"]},
                "monthly_limit": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
        },
    },
]

INSTRUCTIONS = (
    "Use exactly one registered finance tool for each supported synthetic request. "
    "Return the tool result verbatim. Never claim or attempt a transfer, payment, or other "
    "financial action. If a transfer is requested, explain that transfers are unavailable."
)


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


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    if name == "account_lookup":
        return account_lookup(str(arguments["account_id"]))
    if name == "transaction_search":
        return transaction_search(
            str(arguments["account_id"]),
            str(arguments["category"]),
        )
    if name == "budget_calculation":
        return budget_calculation(
            str(arguments["account_id"]),
            int(arguments["monthly_limit"]),
        )
    raise ValueError(f"Unsupported finance tool: {name}")
