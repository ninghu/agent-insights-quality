from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

import yaml
from agent_insights_quality.util import ROOT


def _is_source_file(path: Path) -> bool:
    return (
        path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
    )


def test_prompt_definitions_are_complete_and_use_terra() -> None:
    paths = sorted(
        [
            *ROOT.glob("agents/weather-agent/**/definition.json"),
            *ROOT.glob("agents/healthcare-agent/**/definition.json"),
        ]
    )
    assert len(paths) == 14
    digests = set()
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        definition = value["definition"]
        assert definition["kind"] == "prompt"
        assert definition["model"] == "gpt-5.6-terra"
        assert definition["instructions"].strip()
        assert definition["tools"]
        logical_version = "v0" if path.parent.name == "v0" else path.parent.name
        if logical_version != "v0":
            assert value["metadata"]["logical_version"] == logical_version
        serialized = json.dumps(value, sort_keys=True)
        assert '"injection"' not in serialized
        assert '"mode"' not in serialized
        digests.add(hashlib.sha256(path.read_bytes()).hexdigest())
    assert len(digests) == 14


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
                assert not any(
                    "__pycache__" in Path(name).parts
                    or Path(name).suffix.casefold() in {".pyc", ".pyo"}
                    for name in names
                )
                active = yaml.safe_load(archive.read("issue.yaml"))
                issue_id = implementation.parent.name.replace(
                    "implementation", "v0"
                )
                assert active["issue_id"] == issue_id
                app = archive.read("source/app.py")
                source_root = (
                    root / "v0" / "source"
                    if issue_id == "v0"
                    else implementation.parent / "source"
                )
                assert app == (source_root / "app.py").read_bytes()
                text = app.decode("utf-8")
                assert "AIQ-PATCH-" not in text
                assert "mode ==" not in text
        assert len(digests) == 9


def test_hosted_packagers_fail_when_issue_source_is_missing(tmp_path: Path) -> None:
    for agent_name in ("finance-agent", "travel-agent", "support-ticket-agent"):
        package = ROOT / "agents" / agent_name / "v0" / "package.py"
        issue_root = tmp_path / agent_name
        issue_root.mkdir()
        implementation = issue_root / "implementation.yaml"
        implementation.write_text(
            f"issue_id: issue-999\nagent_name: {agent_name}\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                "python",
                str(package),
                "--issue",
                str(implementation),
                "--output",
                str(issue_root / "package.zip"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode != 0
        assert "source tree is missing" in completed.stderr


def test_every_hosted_issue_has_self_contained_source() -> None:
    for agent_name in ("finance-agent", "travel-agent", "support-ticket-agent"):
        root = ROOT / "agents" / agent_name
        baseline_files = {
            path.relative_to(root / "v0" / "source").as_posix()
            for path in (root / "v0" / "source").rglob("*")
            if _is_source_file(path)
        }
        app_digests = set()
        for issue in sorted(
            path for path in (root / "issues").iterdir() if path.is_dir()
        ):
            issue_files = {
                path.relative_to(issue / "source").as_posix()
                for path in (issue / "source").rglob("*")
                if _is_source_file(path)
            }
            assert issue_files == baseline_files
            app = (issue / "source" / "app.py").read_bytes()
            assert b"AIQ-PATCH-" not in app
            assert b"mode ==" not in app
            app_digests.add(hashlib.sha256(app).hexdigest())
        assert len(app_digests) == 8


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
    issue_014 = (
        ROOT
        / "agents"
        / "finance-agent"
        / "issues"
        / "issue-014"
        / "source"
        / "app.py"
    ).read_text(encoding="utf-8")
    assert "str | None" in issue_014
    assert "] = None" in issue_014


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
