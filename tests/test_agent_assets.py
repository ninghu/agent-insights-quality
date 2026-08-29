from __future__ import annotations

import difflib
import hashlib
import importlib.util
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


def _load_travel_options():
    path = ROOT / "agents" / "travel-agent" / "v0" / "source" / "options.py"
    spec = importlib.util.spec_from_file_location("travel_options_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        assert "tools" not in definition
        if "healthcare-agent" in path.parts:
            instructions = definition["instructions"]
            assert "request-provided synthetic data" in instructions
            assert "you have no tools" in instructions
            assert "exactly one direct final response" in instructions
        if "weather-agent" in path.parts:
            instructions = definition["instructions"]
            assert "request-provided synthetic data" in instructions
            assert "you have no tools" in instructions
            assert "exactly one direct final response" in instructions
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
    assert request_count == 210
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
            "    state = {\n"
            '        "ticket_id": ticket_id,\n'
            '        "revision": TICKETS[ticket_id]["revision"],\n'
            "    }\n"
            '    with tracer.start_as_current_span("support.state.propagation") as span:\n'
            '        span.set_attribute("state.keys_before", len(state))\n'
            "        state.clear()\n"
            '        span.set_attribute("state.keys_after", len(state))\n'
            '    read_ticket_id = state.get("ticket_id")\n'
            "    read_result = tool(\n"
            '        "read_ticket",\n'
            "        {\n"
            '            "ok": read_ticket_id is not None,\n'
            '            "ticket_id": read_ticket_id,\n'
            '            "error": (\n'
            "                None\n"
            "                if read_ticket_id is not None\n"
            '                else {"code": "ticket_id_missing"}\n'
            "            ),\n"
            "        },\n"
            "    )\n"
            '    expected_revision = state.get("revision")\n'
            "    update_result = tool(\n"
            '        "update_ticket",\n'
            "        {\n"
            '            "ok": expected_revision is not None,\n'
            '            "expected_revision": expected_revision,\n'
            '            "error": (\n'
            "                None\n"
            "                if expected_revision is not None\n"
            '                else {"code": "revision_missing"}\n'
            "            ),\n"
            "        },\n"
            "    )\n"
            "    symptoms = []\n"
            '    if not read_result["ok"]:\n'
            '        symptoms.append("ticket routing failed because the ticket identifier was lost")\n'
            '    if not update_result["ok"]:\n'
            '        symptoms.append("ticket update failed because the revision was lost")\n'
            '    return "Shared state propagation failed: " + "; ".join(symptoms) + "."\n'
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
        options = (source.parent / "options.py").read_text(encoding="utf-8")
        assert "MAX_RESPONSE_OPTIONS = 2" in options
        assert '"carrier": "Contoso Air"' in text
        assert '"departure": "09:00"' in text
        assert '"property": "Fabrikam Stay"' in text
        assert '"rating": 4.5' in text
        assert "count = 80 if include_details else 2" in text
        assert "def bounded_inventory_options(" in options
        assert "selected_kinds" in options
        assert "def first_option_per_itinerary(" in options
        assert "def requested_inventory_kind(" in options
        assert 'option["source_id"] = option["id"]' in options
        assert 'option["id"] = f"{option[\'trip\']}-{option[\'id\']}"' in options
        assert 'if len(trips) >= 2 and "compare" in text:' in text
        assert "search_operation(item, include_details)" in text
        if "issue-026" not in source.parts:
            assert 'return {"inventory": first_option_per_itinerary(branches)}' in text
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


def test_travel_bounded_inventory_keeps_one_representative_per_kind() -> None:
    options = _load_travel_options()
    inventory = [
        {"id": "flight-demo-0", "kind": "flight"},
        {"id": "flight-demo-1", "kind": "flight"},
        {"id": "hotel-demo-0", "kind": "hotel"},
        {"id": "hotel-demo-1", "kind": "hotel"},
    ]

    assert [
        option["id"] for option in options.bounded_inventory_options(inventory)
    ] == ["flight-demo-0", "hotel-demo-0"]
    assert [
        option["id"]
        for option in options.bounded_inventory_options(inventory[:2])
    ] == ["flight-demo-0"]


def test_travel_partial_inventory_returns_one_useful_option() -> None:
    options = _load_travel_options()
    partial_inventory = [
        {
            "id": "flight-demo-0",
            "kind": "flight",
            "trip": "trip-beta",
            "carrier": "Contoso Air",
            "departure": "09:00",
            "price": 200,
        },
        {
            "id": "flight-demo-1",
            "kind": "flight",
            "trip": "trip-beta",
            "carrier": "Contoso Air",
            "departure": "09:00",
            "price": 201,
        },
    ]

    response_options = options.bounded_inventory_options(partial_inventory)
    assert len(response_options) == 1
    assert options.describe_itineraries(partial_inventory) == "Itinerary trip-beta"
    assert options.describe_inventory(response_options) == (
        "Flight flight-demo-0 for trip-beta: carrier Contoso Air, "
        "departure 09:00, price USD 200"
    )


def test_travel_two_trip_comparison_preserves_both_itineraries() -> None:
    options = _load_travel_options()
    prompt = "Compare flight options for trip-alpha and trip-beta."
    trips = options.requested_trips(prompt)
    branches = [
        [
            {
                "id": "flight-demo-0",
                "kind": "flight",
                "trip": trip,
                "carrier": "Contoso Air",
                "departure": "09:00",
                "price": 200,
            },
            {
                "id": "flight-demo-1",
                "kind": "flight",
                "trip": trip,
                "carrier": "Contoso Air",
                "departure": "11:00",
                "price": 210,
            },
        ]
        for trip in trips
    ]

    inventory = options.first_option_per_itinerary(branches)
    assert trips == ["trip-alpha", "trip-beta"]
    assert [option["trip"] for option in inventory] == trips
    assert [option["id"] for option in inventory] == [
        "trip-alpha-flight-demo-0",
        "trip-beta-flight-demo-0",
    ]
    assert options.describe_itineraries(inventory) == (
        "Compared itineraries trip-alpha and trip-beta"
    )
    rendered = options.describe_inventory(
        options.bounded_inventory_options(inventory)
    )
    assert "trip-alpha-flight-demo-0 for trip-alpha" in rendered
    assert "trip-beta-flight-demo-0 for trip-beta" in rendered
    assert (
        options.requested_inventory_kind(
            "Compare hotel options for trip-alpha and trip-beta."
        )
        == "hotel"
    )
    hotel_branches = [
        [
            {
                "id": "hotel-demo-0",
                "kind": "hotel",
                "trip": trip,
                "property": "Fabrikam Stay",
                "rating": 4.5,
                "price": 120,
            }
        ]
        for trip in trips
    ]
    hotels = options.first_option_per_itinerary(hotel_branches)
    hotel_rendered = options.describe_inventory(
        options.bounded_inventory_options(hotels)
    )
    assert "trip-alpha-hotel-demo-0 for trip-alpha" in hotel_rendered
    assert "trip-beta-hotel-demo-0 for trip-beta" in hotel_rendered


def test_travel_switch_parses_destination_without_comparison() -> None:
    options = _load_travel_options()
    traffic = json.loads(
        (
            ROOT
            / "agents"
            / "travel-agent"
            / "issues"
            / "issue-028"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    requests = traffic["requests"]
    conversation_ids = {
        request["request"]["body"]["conversation"]["id"] for request in requests
    }
    assert len(requests) == 5
    assert len(conversation_ids) == 5
    for request in requests:
        request_text = request["request"]["body"]["input"][0]["content"][0]["text"]
        assert "compare" not in request_text.lower()
        source_text = request_text.lower().split(" to ", 1)[0]
        source_trip = options.requested_trips(source_text)[0]
        destination_trip = options.parse_trip(request_text)
        assert source_trip != destination_trip
        assert request["expected"]["defect_observed"] is True
        assert request["expected"]["activation_gate"] is True
        assertions = request["expected"]["semantic_assertions"]
        assert source_trip in assertions["required_terms_all"]
        assert destination_trip in assertions["forbidden_terms"]
        assert all(
            assertion["kind"] == "scope_relation"
            and assertion["scope_kind"] == "trip"
            and assertion["request_scope"] == "last"
            and assertion["request_tool_equal"] is False
            for assertion in request["expected"]["trace_assertions"]
        )

    source = (
        ROOT
        / "agents"
        / "travel-agent"
        / "issues"
        / "issue-028"
        / "source"
        / "app.py"
    ).read_text(encoding="utf-8")
    assert 'lowered.split(" to ", 1)[0]' in source
    assert "if source_trips:" in source
    assert 'if state.get("trip")' not in source


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

    issue_023_root = root / "issues" / "issue-023"
    issue_023_implementation = yaml.safe_load(
        (issue_023_root / "implementation.yaml").read_text(encoding="utf-8")
    )
    assert issue_023_implementation["category"] == "tool_call_failures"
    assert issue_023_implementation["expected_behavior"] == {
        "desired": "Run authoritative inventory search before answering.",
        "injected": (
            "Returns a truthful no-inventory answer without the required inventory "
            "search."
        ),
    }
    issue_023_source = (issue_023_root / "source" / "app.py").read_text(
        encoding="utf-8"
    )
    assert issue_023_source.count('return {"inventory": []}') == 1
    assert "Inventory is available even though no authoritative search ran." not in (
        issue_023_source
    )
    issue_023_traffic = json.loads(
        (issue_023_root / "traffic.json").read_text(encoding="utf-8")
    )
    for request in issue_023_traffic["requests"]:
        expected = request["expected"]
        assert expected["activation_gate"] is True
        assert expected["semantic_assertions"] == {
            "required_terms_all": [
                "No itinerary",
                "No synthetic inventory options",
                "Booking not completed",
                "Showing 0 of 0 synthetic options",
            ],
            "forbidden_terms": ["available"],
        }


def test_travel_021_through_027_outcomes_remain_isolated() -> None:
    root = ROOT / "agents" / "travel-agent" / "issues"
    markers = {
        "issue-021": [
            'await failed_search("search_flights")',
            'answer = "A seat is available on invented-demo-seat."',
        ],
        "issue-022": ['if "flight" in text:', "await search_hotels(trip)"],
        "issue-023": ['return {"inventory": []}'],
        "issue-024": ["include_details = True"],
        "issue-025": [
            'return {"inventory": inventory, "booked": True}',
            "async def book",
            "return {}",
        ],
        "issue-026": ['item["trip"] == trips[0]'],
        "issue-027": [
            "flights = await search_flights(trip)",
            "hotels = await search_hotels(trip)",
        ],
    }
    for issue_id, required in markers.items():
        source = (root / issue_id / "source" / "app.py").read_text(
            encoding="utf-8"
        )
        assert all(marker in source for marker in required)
        traffic = json.loads(
            (root / issue_id / "traffic.json").read_text(encoding="utf-8")
        )
        assert len(traffic["requests"]) == 5
        assert all(
            request["expected"]["defect_observed"] is True
            for request in traffic["requests"]
        )


def test_finance_issue_sources_match_reviewed_deltas() -> None:
    root = ROOT / "agents" / "finance-agent"
    baseline_root = root / "v0" / "source"
    manifest = yaml.safe_load(
        (ROOT / "tests" / "fixtures" / "finance_issue_source_deltas.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["contract_version"] == "1.0"
    assert manifest["baseline"] == "agents/finance-agent/v0/source"
    issues = manifest["issues"]
    assert set(issues) == {
        "issue-013",
        "issue-014",
        "issue-015",
        "issue-016",
        "issue-017",
        "issue-018",
        "issue-019",
        "issue-020",
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
                actual_diff = "\n".join(
                    line for line in actual_diff.splitlines() if line != " "
                )
                assert actual_diff == reviewed["expected_app_diff"]
            else:
                assert issue_files[relative_path].read_bytes() == baseline_path.read_bytes()

    for issue_id in ("issue-016", "issue-017", "issue-019"):
        traffic = json.loads(
            (root / "issues" / issue_id / "traffic.json").read_text(encoding="utf-8")
        )
        request_texts = [
            request["request"]["body"]["input"][0]["content"][0]["text"]
            for request in traffic["requests"]
        ]
        assert all("acct-demo-missing" in text for text in request_texts)

    required_trace_assertions = {
        "issue-013": {"one_balance_call", "authoritative_balance_contradicted"},
        "issue-014": {
            "one_balance_call",
            "account_id_omitted",
            "missing_argument_error",
        },
        "issue-015": {"one_balance_call", "opposite_account_scope"},
        "issue-016": {"one_balance_call", "error_presented_as_balance"},
        "issue-017": {"two_budget_calls", "mixed_result_claimed_complete"},
        "issue-018": {
            "one_transient_attempt",
            "retry_sequence_stops_on_error",
        },
        "issue-019": {
            "three_permanent_attempts",
            "permanent_error_retry_sequence",
        },
        "issue-020": {"model_context_repeated_four_times"},
    }
    for issue_id, required_names in required_trace_assertions.items():
        issue_root = root / "issues" / issue_id
        traffic = json.loads(
            (issue_root / "traffic.json").read_text(encoding="utf-8")
        )
        assert len(traffic["requests"]) == 5
        for request in traffic["requests"]:
            expected = request["expected"]
            assert expected["activation_gate"] is True
            assert {
                assertion["name"]
                for assertion in expected["trace_assertions"]
            } == required_names
            if issue_id != "issue-020":
                assert expected["semantic_assertions"]

    deterministic_middleware = {
        "issue-013": "ContradictedBalance",
        "issue-014": "MissingAccountIdentifier",
        "issue-015": "OppositeAccountScope",
        "issue-016": "StructuredErrorAsBalance",
        "issue-017": "CompletePartialAggregate",
        "issue-018": "MissingTransientRetry",
        "issue-019": "PermanentFailureRetryLoop",
        "issue-020": "DuplicateContext",
    }
    request_predicates = {
        "issue-013": ("show the balance", "changed = result"),
        "issue-014": ("show the balance", 'result = {"ok": False'),
        "issue-015": ("show its balance", "requested ="),
        "issue-016": ("preserve the tool error", 'result = {"ok": False'),
        "issue-017": ("complete budget summary", "results ="),
        "issue-018": ("transient balance lookup", "result = {"),
        "issue-019": ("show the balance", "arguments ="),
        "issue-020": (
            "summarize the balance and monthly items",
            "context.messages.extend(original * 3)",
        ),
    }
    for issue_id, class_name in deterministic_middleware.items():
        source = (
            root / "issues" / issue_id / "source" / "app.py"
        ).read_text(encoding="utf-8")
        assert f"class {class_name}(ChatMiddleware):" in source
        assert f"middleware = [{class_name}()]" in source
        predicate, activation = request_predicates[issue_id]
        assert predicate in source
        assert "del call_next" not in source
        assert source.index("await call_next()") < source.index(activation)


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
        assert "output_present = bool(result.strip())" in text
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
    assert "str | None" not in issue_014
    assert "] = None" not in issue_014
    assert 'Field(description="Required synthetic account identifier.")' in issue_014


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
    ] == {
        "required_terms_all": ["succeeded", "retry"],
        "forbidden_claims": [
            "retry failed",
            "did not succeed",
            "recovery failed",
        ],
    }
    assert expected_by_id["support-ticket-agent-v0-partial"][
        "semantic_assertions"
    ] == {
        "exact_text": (
            "Ticket ID ticket-demo-1; revision 3; status open; "
            "summary Synthetic printer setup; optional history unavailable."
        ),
    }


def test_prompt_traffic_has_no_fixtures_and_has_reviewed_assertions() -> None:
    paths = sorted(
        [
            *ROOT.glob("agents/weather-agent/**/traffic.json"),
            *ROOT.glob("agents/healthcare-agent/**/traffic.json"),
        ]
    )
    assert len(paths) == 14
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        assert all("tool_fixtures" not in item for item in value["requests"])
        if path.parent.name == "v0":
            assert all(
                item["expected"].get("semantic_assertions")
                for item in value["requests"]
            )
        else:
            activation = [
                item
                for item in value["requests"]
                if item["expected"].get("activation_gate") is True
            ]
            assert activation
            assert all(
                item["expected"].get("semantic_assertions")
                for item in activation
            )


def test_prompt_json_contracts_are_evaluator_side_and_tool_free() -> None:
    paths = sorted(
        [
            *ROOT.glob("agents/weather-agent/**/traffic.json"),
            *ROOT.glob("agents/healthcare-agent/**/traffic.json"),
        ]
    )
    forbidden = {
        "function_call",
        "functions",
        "parallel_tool_calls",
        "tool",
        "tool_choice",
        "tool_config",
        "tool_configs",
        "tool_fixtures",
        "tool_resources",
        "tools",
    }

    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(child) for child in value))
        return set()

    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        for item in value["requests"]:
            body = item["request"]["body"]
            assert forbidden.isdisjoint(keys(body))
            assert "text" not in body

    weather = json.loads(
        (ROOT / "agents" / "weather-agent" / "v0" / "traffic.json").read_text(
            encoding="utf-8"
        )
    )["requests"][3]["expected"]["semantic_assertions"]
    assert weather["response_format"] == "json"
    assert weather["exact_json_fields"] == {"temperature": 21}
    assert weather["casefold_json_fields"] == {
        "condition": "clear",
        "unit": "celsius",
    }
    weather_schema = weather["json_schema"]
    assert weather_schema["type"] == "object"
    assert weather_schema["additionalProperties"] is False
    assert set(weather_schema["required"]) == set(weather_schema["properties"]) == {
        "condition",
        "temperature",
        "unit",
    }
    assert {
        key: value["type"]
        for key, value in weather_schema["properties"].items()
    } == {
        "condition": "string",
        "temperature": "integer",
        "unit": "string",
    }

    healthcare = json.loads(
        (ROOT / "agents" / "healthcare-agent" / "v0" / "traffic.json").read_text(
            encoding="utf-8"
        )
    )["requests"][4]["expected"]["semantic_assertions"]
    healthcare_schema = healthcare["json_schema"]
    assert healthcare["response_format"] == "json"
    assert healthcare_schema["type"] == "object"
    assert healthcare_schema["additionalProperties"] is False
    assert set(healthcare_schema["required"]) == set(
        healthcare_schema["properties"]
    ) == set(healthcare["exact_json"])
    assert {
        value["type"] for value in healthcare_schema["properties"].values()
    } == {"string"}

    issue_002 = json.loads(
        (
            ROOT
            / "agents"
            / "weather-agent"
            / "issues"
            / "issue-002"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    assert all("text" not in item["request"]["body"] for item in issue_002["requests"])


def test_prompt_activation_gates_are_request_bound() -> None:
    structured = {
        "issue-001",
        "issue-003",
        "issue-004",
        "issue-005",
        "issue-007",
        "issue-008",
        "issue-009",
        "issue-010",
        "issue-011",
        "issue-012",
    }
    for issue_id in structured:
        agent = "weather-agent" if int(issue_id[-3:]) <= 6 else "healthcare-agent"
        value = json.loads(
            (
                ROOT
                / "agents"
                / agent
                / "issues"
                / issue_id
                / "traffic.json"
            ).read_text(encoding="utf-8")
        )
        gates = [
            item
            for item in value["requests"]
            if item["expected"].get("activation_gate") is True
        ]
        assert gates
        assertion_key = "exact_text" if issue_id == "issue-005" else "exact_json"
        assert all(
            assertion_key in item["expected"]["semantic_assertions"]
            for item in gates
        )
    verbose = json.loads(
        (
            ROOT
            / "agents"
            / "weather-agent"
            / "issues"
            / "issue-006"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    exact_verbose = verbose["requests"][0]["expected"]["semantic_assertions"][
        "exact_text"
    ]
    assert len(re.findall(r"\S+", exact_verbose)) == 82
    assert all(
        item["expected"]["semantic_assertions"] == {
            "exact_text": exact_verbose,
            "min_words": 80,
        }
        for item in verbose["requests"]
    )
    schema_violation = json.loads(
        (
            ROOT
            / "agents"
            / "weather-agent"
            / "issues"
            / "issue-002"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    assert all(
        item["expected"]["semantic_assertions"] == {
            "response_format": "non_json",
            "exact_text": "Weather summary: clear, 21 celsius.",
        }
        for item in schema_violation["requests"]
    )


def test_weather_latency_issue_requires_five_two_turn_groups() -> None:
    value = json.loads(
        (
            ROOT
            / "agents"
            / "weather-agent"
            / "issues"
            / "issue-005"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    conversations: dict[str, list[dict]] = {}
    for item in value["requests"]:
        conversation = item["request"]["body"]["conversation"]["id"]
        conversations.setdefault(conversation, []).append(item)
    assert len(value["requests"]) == 10
    assert len(conversations) == 5
    for turns in conversations.values():
        assert len(turns) == 2
        first, second = turns
        first_text = json.dumps(first["request"]["body"])
        second_text = json.dumps(second["request"]["body"])
        assert "condition=clear" in first_text
        assert "temperature=20" in first_text
        assert "already gave" in second_text
        assert first["expected"]["semantic_assertions"]["exact_text"] == (
            "Would you like me to use the complete weather evidence already provided?"
        )
        assert first["expected"]["activation_gate"] is True
        assert first["expected"]["defect_observed"] is True
        assert second["expected"]["semantic_assertions"]["exact_json"] == {
            "phase": "answer_complete",
            "completed": True,
            "condition": "clear",
            "temperature": 20,
            "unit": "celsius",
        }
        assert second["expected"]["activation_gate"] is False


def test_healthcare_action_issues_emit_distinct_json_envelopes() -> None:
    for issue_id, action in {
        "issue-008": "create_appointment",
        "issue-011": "transition_appointment_state",
    }.items():
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
            assertions = item["expected"]["semantic_assertions"]
            assert assertions["response_format"] == "json"
            envelope = assertions["exact_json"]
            assert envelope["action"] == action
            assert item["expected"]["activation_gate"] is True
            if issue_id == "issue-008":
                assert set(envelope) == {
                    "action",
                    "provider",
                    "slot",
                    "message",
                }
                assert envelope["provider"] == "Dr. Rivera"
                assert envelope["message"] == "Please confirm"
            else:
                assert {
                    "action",
                    "provider",
                    "slot",
                    "account_scope",
                    "confirmation",
                    "state",
                } == set(envelope)
                assert envelope["provider"] == "Dr. Rivera"
                assert envelope["account_scope"] == "demo-account-a"
                assert envelope["confirmation"] is False
                assert envelope["state"] == "confirmed"
                text = item["request"]["body"]["input"][0]["content"][0]["text"]
                assert "transition_appointment_state" in text
                assert "Explicit confirmation for this transition has not been granted" in text
    baseline = json.loads(
        (
            ROOT / "agents" / "healthcare-agent" / "v0" / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    assert baseline["requests"][4]["expected"]["semantic_assertions"][
        "exact_json"
    ] == {
        "action": "create_appointment",
        "provider": "Dr. Ali",
        "slot": "slot-demo-505",
        "account_scope": "demo-account-b",
        "approval": "pending",
        "message": "Please confirm",
        "warning": "2026-09-15 unavailable",
    }
    baseline_instructions = json.loads(
        (
            ROOT / "agents" / "healthcare-agent" / "v0" / "definition.json"
        ).read_text(encoding="utf-8")
    )["definition"]["instructions"]
    assert '"2026-09-15 unavailable"' in baseline_instructions
    guardrail_text = baseline["requests"][4]["request"]["body"]["input"][0][
        "content"
    ][0]["text"]
    assert "no schedule evidence is available for 2026-09-15" in guardrail_text


def test_healthcare_baseline_assertions_reject_contradictory_answers() -> None:
    value = json.loads(
        (
            ROOT / "agents" / "healthcare-agent" / "v0" / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    grounded, bounded, memory, correction, guardrail = value["requests"]
    assert [
        item["expected"]["semantic_assertions"]["exact_text"]
        for item in (grounded, bounded, memory, correction)
    ] == [
        (
            "demo-account-a: Dr. Rivera has slot-demo-101 open at 09:30 "
            "on 2026-09-10."
        ),
        (
            "demo-account-b: Dr. Chen has slot-demo-202 open at 15:00 "
            "on 2026-09-11."
        ),
        (
            "demo-account-a: Dr. Rivera has slot-demo-303 open at 11:00 "
            "on 2026-09-12."
        ),
        (
            "demo-account-a: Dr. Chen has slot-demo-311 open at 14:00 "
            "on 2026-09-12."
        ),
    ]
    for item in (grounded, bounded, memory, correction):
        exact_text = item["expected"]["semantic_assertions"]["exact_text"]
        request_text = item["request"]["body"]["input"][0]["content"][0]["text"]
        assert request_text.endswith(f"Return exactly: {exact_text}")
    assert guardrail["expected"]["semantic_assertions"]["exact_json"] == {
        "action": "create_appointment",
        "provider": "Dr. Ali",
        "slot": "slot-demo-505",
        "account_scope": "demo-account-b",
        "approval": "pending",
        "message": "Please confirm",
        "warning": "2026-09-15 unavailable",
    }
    assert "confirmation_prompt" not in guardrail["expected"]["behavior"]


def test_healthcare_unsupported_availability_is_request_bound() -> None:
    value = json.loads(
        (
            ROOT
            / "agents"
            / "healthcare-agent"
            / "issues"
            / "issue-010"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    for item in value["requests"]:
        text = item["request"]["body"]["input"][0]["content"][0]["text"]
        envelope = item["expected"]["semantic_assertions"]["exact_json"]
        assert set(envelope) == {
            "availability",
            "provider",
            "date",
            "slot",
            "account_scope",
            "evidence",
        }
        assert envelope["availability"] == "available"
        assert envelope["evidence"] == "missing"
        assert all(
            envelope[field] in text
            for field in ("provider", "date", "slot", "account_scope")
        )
    afternoon = value["requests"][2]
    assert "15:00" in afternoon["request"]["body"]["input"][0]["content"][0]["text"]
    assert afternoon["expected"]["semantic_assertions"]["exact_json"]["slot"] == (
        "slot-demo-303"
    )


def test_healthcare_alignment_roots_match_exact_traffic() -> None:
    issue_root = ROOT / "agents" / "healthcare-agent" / "issues"
    handoff = yaml.safe_load(
        (issue_root / "issue-007" / "implementation.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert '{"handoff":"scheduling","status":"unavailable"}' in (
        handoff["injected_defect"]["single_root"]
    )

    transition_files = [
        issue_root / "issue-011" / name
        for name in ("definition.json", "implementation.yaml", "traffic.json")
    ]
    transition = "\n".join(
        path.read_text(encoding="utf-8") for path in transition_files
    )
    assert "explicitly unconfirmed transition" in transition
    assert "even though the user only expressed interest" not in transition
    assert "Treat any question about" not in transition


def test_prompt_substitution_issues_bind_supplied_evidence() -> None:
    weather = json.loads(
        (
            ROOT
            / "agents"
            / "weather-agent"
            / "issues"
            / "issue-003"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    for item in weather["requests"]:
        text = json.dumps(item["request"]["body"])
        assert "high=27" in text and "low=13" in text
        assert item["expected"]["semantic_assertions"]["exact_json"] == {
            "shape": "forecast",
            "high": 27,
            "low": 13,
            "unit": "celsius",
        }
    healthcare = json.loads(
        (
            ROOT
            / "agents"
            / "healthcare-agent"
            / "issues"
            / "issue-012"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    for index, item in enumerate(healthcare["requests"], start=1):
        expected = item["expected"]["semantic_assertions"]["exact_json"]
        text = json.dumps(item["request"]["body"])
        assert expected["record_id"] in text
        assert expected["slot"] in text
        assert expected["account_scope"] == "demo-account-b"


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
    assert all(
        "2026-09-21" in json.dumps(item["request"]["body"])
        for item in value["requests"]
    )
