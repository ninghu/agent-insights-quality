"""Pure Finance domain behavior; no Hosted SDK, cloud, or extracted source functions."""

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest


ROOT = Path(__file__).resolve().parents[2] / "agents" / "finance-agent"
VERSIONS = ["v0", *(f"issue-{number:03d}" for number in range(13, 21))]


@pytest.fixture(params=VERSIONS)
def domain(request):
    directory = ROOT / "v0" if request.param == "v0" else ROOT / "issues" / request.param
    spec = importlib.util.spec_from_file_location(
        f"finance_domain_{request.param}", directory / "source" / "tools.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_account_scope_and_returned_data_are_independent(domain):
    first = domain.get_balance("acct-demo-a")
    second = domain.get_balance("acct-demo-b")
    assert first == {
        "ok": True, "account_id": "acct-demo-a", "balance": 1250.50,
        "currency": "USD", "spend": 430.25,
    }
    assert second["account_id"] == "acct-demo-b"
    assert second["balance"] == 875.00
    first["balance"] = 0
    assert domain.get_balance("acct-demo-a")["balance"] == 1250.50


@pytest.mark.parametrize("name", ["get_balance", "get_budget_summary", "list_monthly_items"])
def test_missing_accounts_are_scoped_permanent_errors(domain, name):
    lookup = getattr(domain, name)
    assert lookup("acct-demo-missing") == {
        "ok": False, "account_id": "acct-demo-missing",
        "error": {"code": "account_not_found"},
    }
    assert lookup("") == {"ok": False, "error": {"code": "account_id_required"}}


def test_budget_and_items_use_the_requested_account(domain):
    assert domain.get_budget_summary("acct-demo-b") == {
        "ok": True, "account_id": "acct-demo-b",
        "monthly_limit": 1000.0, "spent": 210.0, "currency": "USD",
    }
    result = domain.list_monthly_items("acct-demo-a")
    assert result["account_id"] == "acct-demo-a"
    assert [item["amount"] for item in result["items"]] == [45.0, 132.5, 88.0]
    result["items"].clear()
    assert len(domain.list_monthly_items("acct-demo-a")["items"]) == 3


def test_transient_is_once_per_account_not_an_alternating_failure(domain):
    lookup = domain.TransientBalances()
    for account in ("acct-demo-a", "acct-demo-b"):
        assert lookup.get_balance(account) == {
            "ok": False, "account_id": account,
            "error": {"code": "temporary_unavailable", "retryable": True},
        }
        for _ in range(3):
            assert lookup.get_balance(account) == domain.get_balance(account)
    assert domain.TransientBalances().get_balance("acct-demo-a")["ok"] is False


def test_unknown_transient_account_never_raises_or_becomes_retryable(domain):
    lookup = domain.TransientBalances()
    for _ in range(3):
        assert lookup.get_balance("acct-demo-missing") == domain.get_balance("acct-demo-missing")
        assert lookup.get_balance("") == domain.get_balance("")


def test_simultaneous_transient_calls_have_exactly_one_first_failure(domain):
    lookup = domain.TransientBalances()
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        return lookup.get_balance("acct-demo-a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(result["ok"] for result in results) == [False, True]
