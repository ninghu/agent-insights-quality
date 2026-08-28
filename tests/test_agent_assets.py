from __future__ import annotations

import difflib
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


def _normalized_source_diff(baseline: str, issue: str) -> str:
    lines = difflib.unified_diff(
        baseline.splitlines(),
        issue.splitlines(),
        fromfile="baseline",
        tofile="issue",
        n=1,
        lineterm="",
    )
    normalized = []
    for line in lines:
        if line.startswith(("--- ", "+++ ")):
            continue
        normalized.append(
            re.sub(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", "@@", line)
        )
    return "\n".join(normalized)


def test_prompt_definitions_are_complete_and_use_gpt_5_4_mini() -> None:
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
        assert definition["model"] == "gpt-5.4-mini"
        assert definition["instructions"].strip()
        assert definition["tools"]
        if "healthcare-agent" in path.parts:
            instructions = definition["instructions"]
            assert (
                "call lookup_slots exactly once more using the same account_scope, "
                "provider, and date"
            ) in instructions
            assert "After that retry, do not retry again" in instructions
            assert "wait for every tool response" in instructions
            assert "always emit one final user-facing availability summary" in instructions
        if "weather-agent" in path.parts:
            instructions = definition["instructions"]
            assert "always emit one terminal user-facing response" in instructions
            assert "never finish with tool output only" in instructions
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


def test_support_issue_sources_only_add_their_declared_defect() -> None:
    root = ROOT / "agents" / "support-ticket-agent"
    baseline = (root / "v0" / "source" / "app.py").read_text(encoding="utf-8")
    anchor = "    ticket = tool(\n"
    baseline_prefix, separator, baseline_suffix = baseline.partition(anchor)
    assert separator
    defects = {
        "issue-029": (
            "    for _ in range(2):\n"
            "        tool(\n"
            '            "recover_ticket",\n'
            '            {"ok": False, "error": {"code": "temporary_unavailable"}},\n'
            "        )\n"
            '    return "Recovery was exhausted without escalation."\n'
        ),
        "issue-030": (
            "    updated = tool(\n"
            '        "update_ticket",\n'
            "        {\n"
            '            "ok": True,\n'
            '            "ticket_id": "ticket-demo-1",\n'
            '            "accepted_revision": 2,\n'
            '            "current_revision": 3,\n'
            "        },\n"
            "    )\n"
            '    return f"Update accepted at stale revision '
            '{updated[\'accepted_revision\']}."\n'
        ),
        "issue-031": (
            "    for _ in range(4):\n"
            '        with tracer.start_as_current_span("support.state.waiting"):\n'
            "            pass\n"
            '    return "The request stopped after repeated no-progress states."\n'
        ),
        "issue-032": (
            '    return "Valid ticket request rejected before model or tool dispatch."\n'
        ),
        "issue-033": (
            "    tool(\n"
            '        "read_ticket",\n'
            '        {"ok": True, "ticket_id": ticket_id, "ticket": TICKETS[ticket_id]},\n'
            "    )\n"
            '    return "Ticket data was read, but orchestration stopped before a useful answer."\n'
        ),
        "issue-034": (
            '    with tracer.start_as_current_span("support.model.dispatch") as span:\n'
            '        span.set_attribute("gen_ai.operation.name", "chat")\n'
            '        span.set_attribute("model.ok", False)\n'
            '        span.set_attribute("error.type", "synthetic_model_failure")\n'
            "        span.set_status(Status(StatusCode.ERROR))\n"
            '    return "Synthetic model failure reached the user without bounded recovery."\n'
        ),
        "issue-035": '    return "Update completed successfully."\n',
        "issue-036": (
            '    with tracer.start_as_current_span("support.state.propagation") as span:\n'
            '        span.set_attribute("state.keys_after", 0)\n'
            '    tool("read_ticket", {"ok": False, "error": {"code": "ticket_id_missing"}})\n'
            '    tool("update_ticket", {"ok": False, "error": {"code": "revision_missing"}})\n'
            "    return (\n"
            '        "The shared state lost the ticket identifier and revision, causing routing, "\n'
            '        "tool, and completion failures."\n'
            "    )\n"
        ),
    }
    for issue_id, defect in defects.items():
        source = (
            root / "issues" / issue_id / "source" / "app.py"
        ).read_text(encoding="utf-8")
        expected_prefix = baseline_prefix.replace(
            'ISSUE_ID = "v0"',
            f'ISSUE_ID = "{issue_id}"',
        )
        assert source == expected_prefix + defect + anchor + baseline_suffix


def test_travel_sources_preserve_bounded_comparison_contract() -> None:
    root = ROOT / "agents" / "travel-agent"
    sources = [
        root / "v0" / "source" / "app.py",
        *sorted((root / "issues").glob("issue-*/source/app.py")),
    ]
    assert len(sources) == 9
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "MAX_RESPONSE_OPTIONS = 2" in text
        assert '"carrier": "Contoso Air"' in text
        assert '"departure": "09:00"' in text
        assert '"property": "Fabrikam Stay"' in text
        assert '"rating": 4.5' in text
        assert "count = 80 if include_details else 2" in text
        assert "def bounded_inventory_options(" in text
        assert "Showing {shown} of {len(inventory)} synthetic options." in text
        if "issue-025" not in source.parts:
            assert (
                'return {"booked": bool(state.get("validated") '
                'and state.get("confirmed"))}'
            ) in text
            assert '"booked": True' not in text

    traffic = json.loads((root / "v0" / "traffic.json").read_text(encoding="utf-8"))
    ordinary = next(
        item for item in traffic["requests"] if item["id"] == "travel-agent-v0-ordinary"
    )
    assertions = ordinary["expected"]["semantic_assertions"]
    assert assertions["required_terms_all"] == [
        "flight-demo-0",
        "hotel-demo-0",
        "price",
        "USD 200",
        "USD 120",
        "Booking not completed",
        "Showing 2 of 4",
    ]
    assert assertions["forbidden_terms"] == [
        "Booking completed",
        "flight-demo-1",
        "hotel-demo-1",
    ]


def test_travel_issue_sources_match_reviewed_deltas() -> None:
    root = ROOT / "agents" / "travel-agent"
    baseline_root = root / "v0" / "source"
    manifest = yaml.safe_load(
        (ROOT / "tests" / "fixtures" / "travel_issue_source_deltas.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["contract_version"] == "1.0"
    assert manifest["baseline"] == "agents/travel-agent/v0/source"
    issues = manifest["issues"]
    assert set(issues) == {
        "issue-021",
        "issue-022",
        "issue-023",
        "issue-024",
        "issue-025",
        "issue-026",
        "issue-027",
        "issue-028",
    }

    baseline_files = {
        path.relative_to(baseline_root).as_posix(): path
        for path in baseline_root.rglob("*")
        if _is_source_file(path)
    }
    baseline_app = baseline_files["app.py"].read_text(encoding="utf-8")
    for issue_id, reviewed in issues.items():
        issue_root = root / "issues" / issue_id
        implementation = yaml.safe_load(
            (issue_root / "implementation.yaml").read_text(encoding="utf-8")
        )
        assert (
            reviewed["declared_delta"]
            == implementation["injected_defect"]["single_root"]
        )
        issue_files = {
            path.relative_to(issue_root / "source").as_posix(): path
            for path in (issue_root / "source").rglob("*")
            if _is_source_file(path)
        }
        assert set(issue_files) == set(baseline_files)
        for relative_path, baseline_path in baseline_files.items():
            if relative_path == "app.py":
                actual_diff = _normalized_source_diff(
                    baseline_app,
                    issue_files[relative_path].read_text(encoding="utf-8"),
                )
                assert actual_diff == reviewed["expected_app_diff"]
            else:
                assert issue_files[relative_path].read_bytes() == baseline_path.read_bytes()


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
    support_sources = sorted(
        ROOT.glob("agents/support-ticket-agent/**/source/app.py")
    )
    assert len(support_sources) == 9
    for source in support_sources:
        text = source.read_text(encoding="utf-8")
        assert '"gen_ai.operation.name", "invoke_agent"' in text
        assert '"gen_ai.agent.name", "support-ticket-agent"' in text
        assert '"gen_ai.output.type", "text"' in text
        assert '"gen_ai.response.finish_reasons", ("stop",)' in text
        assert '"aiq.tool.error.handled", True' in text
        assert '"aiq.terminal_response.success", output_succeeded' in text
        assert '"aiq.terminal_response.output_present",' in text
        assert "output_present = bool(result)" in text
        assert "gen_ai.input.messages" not in text
        assert "gen_ai.output.messages" not in text
    assert "transient_lock = threading.Lock()" in finance
    assert "ResetTransientState" not in finance
    finance_sources = sorted(
        ROOT.glob("agents/finance-agent/**/source/app.py")
    )
    assert len(finance_sources) == 9
    for source in finance_sources:
        text = source.read_text(encoding="utf-8")
        assert "transient_attempts: set[tuple[int, str]]" in text
        assert "span.get_span_context().trace_id" in text
        assert "After account_not_found, stop that request" in text
        assert "ContextVar" not in text
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


def test_support_baseline_asserts_handled_error_responses() -> None:
    traffic = json.loads(
        (
            ROOT
            / "agents"
            / "support-ticket-agent"
            / "v0"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    expected_by_id = {
        item["id"]: item["expected"] for item in traffic["requests"]
    }
    assert expected_by_id["support-ticket-agent-v0-transient"][
        "semantic_assertions"
    ] == {"required_terms_all": ["succeeded", "retry"]}
    assert expected_by_id["support-ticket-agent-v0-partial"][
        "semantic_assertions"
    ] == {"required_terms_all": ["ticket", "history", "unavailable"]}


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
