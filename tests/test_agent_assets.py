from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

import yaml

from agent_insights_quality.util import ROOT


def test_prompt_definitions_are_complete_and_use_terra() -> None:
    paths = sorted(
        [
            *ROOT.glob("agents/weather-agent/**/definition.json"),
            *ROOT.glob("agents/healthcare-agent/**/definition.json"),
        ]
    )
    assert len(paths) == 14
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        definition = value["definition"]
        assert definition["kind"] == "prompt"
        assert definition["model"] == "gpt-5.6-terra"
        assert definition["instructions"].strip()
        assert definition["tools"]


def test_all_traffic_is_synthetic_endpoint_traffic() -> None:
    paths = sorted(ROOT.glob("agents/**/traffic.json"))
    assert len(paths) == 41
    request_count = 0
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        assert value["agent_name"].endswith("-agent")
        for item in value["requests"]:
            request_count += 1
            request = item["request"]
            assert request["method"] == "POST"
            assert request["path"] == "/responses"
            assert "input" in request["body"]
    assert request_count == 205
    for path in ROOT.glob("agents/**/implementation.yaml"):
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert value["public_safety"] == {
            "synthetic_only": True,
            "real_people": False,
            "credentials": False,
            "service_locations": False,
        }


def test_hosted_packages_are_deterministic_and_isolated(tmp_path: Path) -> None:
    for agent_name in ("finance-agent", "travel-agent", "support-ticket-agent"):
        root = ROOT / "agents" / agent_name
        package = root / "v0" / "package.py"
        implementations = [
            root / "v0" / "implementation.yaml",
            *sorted((root / "issues").glob("issue-*/implementation.yaml")),
        ]
        assert len(implementations) == 9
        digests = set()
        for index, implementation in enumerate(implementations):
            first = tmp_path / f"{agent_name}-{index}-a.zip"
            second = tmp_path / f"{agent_name}-{index}-b.zip"
            for output in (first, second):
                completed = subprocess.run(
                    [
                        "python",
                        str(package),
                        "--issue",
                        str(implementation),
                        "--output",
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                assert completed.returncode == 0, completed.stderr
            assert first.read_bytes() == second.read_bytes()
            digests.add(hashlib.sha256(first.read_bytes()).hexdigest())
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                assert names.count("issue.yaml") == 1
                assert not any(name.startswith("issues/") for name in names)
                active = yaml.safe_load(archive.read("issue.yaml"))
                assert active["issue_id"] == implementation.parent.name.replace(
                    "implementation", "v0"
                )
        assert len(digests) == 9


def test_hosted_framework_and_identity_boundaries() -> None:
    finance = (
        ROOT / "agents" / "finance-agent" / "v0" / "source" / "app.py"
    ).read_text(encoding="utf-8")
    travel = (
        ROOT / "agents" / "travel-agent" / "v0" / "source" / "app.py"
    ).read_text(encoding="utf-8")
    support = (
        ROOT / "agents" / "support-ticket-agent" / "v0" / "source" / "app.py"
    ).read_text(encoding="utf-8")
    assert "from agent_framework import Agent" in finance
    assert "StateGraph" in travel
    assert "DefaultAzureCredential" in travel
    assert "DefaultAzureCredential" in support
    assert "MODEL_API_KEY" not in support
    assert "ResponsesAgentServerHost" in support
    assert "@app.response_handler" in support
    assert '"gen_ai.operation.name", "execute_tool"' in support
    assert "ContextVar" in finance
    assert "ResetTransientState" not in finance


def test_healthcare_fixture_arguments_match_requested_slots() -> None:
    for issue_id in ("issue-008", "issue-011"):
        value = json.loads(
            (
                ROOT
                / "agents"
                / "healthcare-agent"
                / "issues"
                / issue_id
                / "traffic.json"
            ).read_text(encoding="utf-8")
        )
        for item in value["requests"]:
            text = json.dumps(item["request"]["body"])
            match = re.search(r"slot-demo-[0-9]+", text)
            assert match is not None
            expected_slot = match.group(0)
            fixture_slot = item["tool_fixtures"][0]["arguments"]["slot_id"]
            assert fixture_slot == expected_slot


def test_healthcare_corrections_retain_initial_date() -> None:
    value = json.loads(
        (
            ROOT
            / "agents"
            / "healthcare-agent"
            / "issues"
            / "issue-009"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    dates = {
        item["tool_fixtures"][0]["arguments"]["date"]
        for item in value["requests"]
    }
    assert dates == {"2026-09-21"}
