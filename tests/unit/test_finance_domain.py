"""Pure Finance domain behavior; no Hosted SDK, cloud, or extracted source functions."""

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest


ROOT = Path(__file__).resolve().parents[2] / "agents" / "finance-agent"
VERSIONS = ["v0", *(f"issue-{number:03d}" for number in range(13, 21)), "issue-040"]


def load_domain(version, filename):
    directory = ROOT / "v0" if version == "v0" else ROOT / "issues" / version
    spec = importlib.util.spec_from_file_location(
        f"finance_domain_{version}_{filename}", directory / "source" / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=VERSIONS)
def domain(request):
    return load_domain(request.param, "tools.py")


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


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("description", [
    "a skill-building course", "a skills workshop", "SKILLS training", "upskilling",
])
def test_guardrail_only_overblocks_embedded_restricted_term_in_new_issue(version, description):
    guardrail = load_domain(version, "guardrail.py")
    text = f"Show acct-demo-a budget status for {description}. Only report existing budget data."
    result = guardrail.check_request_content(text)
    blocked = version == "issue-040"
    assert result["decision"] == ("block" if blocked else "allow")
    if blocked:
        assert result["matched_text"].casefold() == "kill"
        assert text[result["match_start"]:result["match_end"]] == result["matched_text"]
        assert text[result["match_start"] - 1].casefold() == "s"
    else:
        assert result["matched_text"] == ""
        assert result["match_start"] == result["match_end"] == -1


@pytest.mark.parametrize("version", VERSIONS)
def test_guardrail_retains_explicit_word_boundary_classification_and_ordinary_budget(version):
    guardrail = load_domain(version, "guardrail.py")
    # Classification-only boundary samples; no request for harmful generated content.
    for sample in ("kill", " KILL ", "(kill)"):
        assert guardrail.check_request_content(sample)["decision"] == "block"
    assert guardrail.check_request_content("Show acct-demo-b budget status.")["decision"] == "allow"
    assert guardrail.check_request_content("")["decision"] == "allow"


def test_all_ten_overblocking_inputs_are_benign_budget_tasks_healthy_versions_allow():
    traffic = json.loads((ROOT / "issues" / "issue-040" / "traffic.json").read_text(encoding="utf-8"))
    requests = {request["id"]: request for request in traffic["requests"]}
    assert [attempt["index"] for attempt in traffic["attempts"]] == list(range(1, 11))
    for attempt in traffic["attempts"]:
        for ref in attempt["probe_steps"]:
            text = requests[ref]["request"]["body"]["input"][0]["content"][0]["text"]
            assert "budget" in text.casefold()
            account = next(account for account in ("acct-demo-a", "acct-demo-b") if account in text)
            for version in VERSIONS:
                assert load_domain(version, "guardrail.py").check_request_content(text)["decision"] == (
                    "block" if version == "issue-040" else "allow"
                )
                assert load_domain(version, "tools.py").get_budget_summary(account)["ok"] is True


def test_baseline_keeps_ten_attempts_and_all_requests_pass_content_guardrail():
    traffic = json.loads((ROOT / "v0" / "traffic.json").read_text(encoding="utf-8"))
    guardrail = load_domain("v0", "guardrail.py")
    assert len(traffic["attempts"]) == 10
    texts = [
        message["text"] for request in traffic["requests"]
        for input_item in request["request"]["body"]["input"]
        for message in input_item["content"]
    ]
    assert any("skill-building" in text for text in texts)
    assert all(guardrail.check_request_content(text)["decision"] == "allow" for text in texts)


@pytest.mark.parametrize("version", VERSIONS[1:-1])
def test_existing_issue_traffic_does_not_acquire_a_second_guardrail_defect(version):
    traffic = json.loads((ROOT / "issues" / version / "traffic.json").read_text(encoding="utf-8"))
    guardrail = load_domain(version, "guardrail.py")
    for request in traffic["requests"]:
        for input_item in request["request"]["body"]["input"]:
            for content in input_item["content"]:
                assert guardrail.check_request_content(content["text"])["decision"] == "allow"
