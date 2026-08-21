from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date

import pytest

import agent_insights_quality.contracts as contracts
from agent_insights_quality.contracts import (
    ContractError,
    EXPECTED_AGENTS,
    REQUIRED_CATEGORIES,
    REQUIRED_SCENARIO_FAMILIES,
    REQUIRED_SEVERITIES,
    ROOT,
    load_agent_manifests,
    load_scenario_catalog,
    validate_daily_plan_semantics,
    validate_instance,
    validate_scenario_catalog_semantics,
    validate_supporting_manifests,
)
from agent_insights_quality.planning import (
    canonical_plan_digest,
    catalog_hash,
    generate_daily_plan,
    render_plan_markdown,
    serialize_plan,
    write_daily_plan,
)


REPORT_DATE = date(2026, 8, 21)


def _inputs() -> tuple[list[dict], dict]:
    agents = load_agent_manifests()
    catalog = load_scenario_catalog(set(EXPECTED_AGENTS))
    return agents, catalog


def test_catalog_has_complete_approved_coverage() -> None:
    _, catalog = _inputs()
    active = [scenario for scenario in catalog["scenarios"] if scenario["status"] == "active"]
    assert len(active) == 63
    assert {scenario["family"] for scenario in active} == REQUIRED_SCENARIO_FAMILIES
    assert {scenario["expected"]["category"] for scenario in active} == REQUIRED_CATEGORIES
    assert {scenario["expected"]["severity"] for scenario in active} == REQUIRED_SEVERITIES
    assert sum(scenario["expected"]["category"] == "none" for scenario in active) == 6
    assert all(scenario["healthy_decoys"] for scenario in active)
    assert all(scenario["evidence"]["negative_controls"] for scenario in active)


def test_catalog_validation_rejects_duplicate_ids_and_missing_references() -> None:
    agents, catalog = _inputs()
    duplicate = deepcopy(catalog)
    duplicate["scenarios"][1]["id"] = duplicate["scenarios"][0]["id"]
    with pytest.raises(ContractError, match="unique"):
        validate_scenario_catalog_semantics(duplicate, agents)

    missing_reference = deepcopy(catalog)
    missing_reference["scenarios"][0]["traffic"]["recipe"] = (
        "scenarios/traffic/not-present.yaml"
    )
    with pytest.raises(ContractError, match="does not exist"):
        validate_scenario_catalog_semantics(missing_reference, agents)

    missing_recipe = deepcopy(catalog)
    missing_recipe["scenarios"][0]["traffic"]["recipe_id"] = "traffic-not-present-v1"
    with pytest.raises(ContractError, match="traffic recipe does not exist"):
        validate_supporting_manifests(missing_recipe)

    wrong_registry = deepcopy(catalog)
    wrong_registry["scenarios"][10]["mutation"]["manifest"] = (
        "scenarios/mutations/prompt-deltas.yaml"
    )
    with pytest.raises(ContractError, match="mutation recipe contract"):
        validate_supporting_manifests(wrong_registry)


def test_catalog_validation_rejects_impossible_compatibility_and_conflicts() -> None:
    agents, catalog = _inputs()
    impossible = deepcopy(catalog)
    impossible["scenarios"][0]["compatibility"] = {
        "domains": ["weather"],
        "agent_types": ["hosted_custom_container"],
        "agent_ids": [],
    }
    with pytest.raises(ContractError, match="cannot match"):
        validate_scenario_catalog_semantics(impossible, agents)

    conflictless = deepcopy(catalog)
    conflictless["scenarios"][0]["conflict_tags"] = []
    with pytest.raises(ContractError, match="conflict tag"):
        validate_scenario_catalog_semantics(conflictless, agents)


def test_catalog_validation_rejects_expected_taxonomy_and_coverage_floor() -> None:
    agents, catalog = _inputs()
    invalid_expected = deepcopy(catalog)
    invalid_expected["scenarios"][0]["expected"]["severity"] = "high"
    with pytest.raises(ContractError, match="none values"):
        validate_scenario_catalog_semantics(invalid_expected, agents)

    below_floor = deepcopy(catalog)
    below_floor["scenarios"][0]["status"] = "retired"
    with pytest.raises(ContractError, match="at least 63"):
        validate_scenario_catalog_semantics(below_floor, agents)


def test_same_inputs_produce_byte_equivalent_plan() -> None:
    agents, catalog = _inputs()
    first = generate_daily_plan(REPORT_DATE, agents=agents, catalog=catalog)
    second = generate_daily_plan(REPORT_DATE, agents=agents, catalog=catalog)
    assert serialize_plan(first) == serialize_plan(second)
    assert first["plan_digest"] == canonical_plan_digest(first)
    assert first["project"]["name"] == first["plan_id"]
    assert first["project"]["expires_on"] == "2026-08-28"


def test_catalog_content_change_changes_hash_seed_and_plan(tmp_path) -> None:
    agents, catalog = _inputs()
    source = (ROOT / "scenarios" / "catalog.yaml").read_bytes()
    changed = source.replace(b'catalog_version: "1.0.0"', b'catalog_version: "1.0.1"', 1)
    changed_path = tmp_path / "catalog.yaml"
    changed_path.write_bytes(changed)
    original_digest = catalog_hash()
    changed_digest = catalog_hash(changed_path)
    original = generate_daily_plan(
        REPORT_DATE,
        agents=agents,
        catalog=catalog,
        catalog_digest=original_digest,
    )
    changed_plan = generate_daily_plan(
        REPORT_DATE,
        agents=agents,
        catalog=catalog,
        catalog_digest=changed_digest,
    )
    assert original_digest != changed_digest
    assert original["seed"] != changed_plan["seed"]
    assert original["plan_digest"] != changed_plan["plan_digest"]


def test_catalog_bundle_hash_changes_with_recipe_or_agent_input(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_path = tmp_path / "scenarios" / "catalog.yaml"
    mutation_path = tmp_path / "scenarios" / "mutations" / "recipes.yaml"
    traffic_path = tmp_path / "scenarios" / "traffic" / "recipes.yaml"
    agent_path = tmp_path / "agents" / "sample" / "manifest.yaml"
    for path in (catalog_path, mutation_path, traffic_path, agent_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value: one\n", encoding="ascii")
    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    first = catalog_hash(catalog_path)
    mutation_path.write_text("value: two\n", encoding="ascii")
    second = catalog_hash(catalog_path)
    agent_path.write_text("value: three\n", encoding="ascii")
    third = catalog_hash(catalog_path)
    assert len({first, second, third}) == 3


def test_planner_rejects_recipe_bound_to_wrong_registry() -> None:
    agents, catalog = _inputs()
    invalid = deepcopy(catalog)
    invalid["scenarios"][10]["mutation"]["manifest"] = (
        "scenarios/mutations/prompt-deltas.yaml"
    )
    with pytest.raises(ContractError, match="mutation recipe contract"):
        generate_daily_plan(REPORT_DATE, agents=agents, catalog=invalid)


def test_plan_covers_each_active_scenario_once_and_is_schema_valid() -> None:
    agents, catalog = _inputs()
    plan = generate_daily_plan(REPORT_DATE, agents=agents, catalog=catalog)
    active_ids = {
        scenario["id"] for scenario in catalog["scenarios"] if scenario["status"] == "active"
    }
    assignment_ids = [assignment["scenario_id"] for assignment in plan["assignments"]]
    assert len(assignment_ids) == len(set(assignment_ids))
    assert set(assignment_ids) == active_ids
    validate_instance(plan, ROOT / "schemas" / "daily-plan.schema.json", "plan")
    validate_daily_plan_semantics(plan, agents, catalog, "plan")


def test_assignments_are_compatible_balanced_and_cover_agent_types() -> None:
    agents, catalog = _inputs()
    plan = generate_daily_plan(REPORT_DATE, agents=agents, catalog=catalog)
    agents_by_id = {agent["id"]: agent for agent in agents}
    scenarios = {scenario["id"]: scenario for scenario in catalog["scenarios"]}
    loads = Counter(assignment["agent_id"] for assignment in plan["assignments"])
    assert set(loads) == set(EXPECTED_AGENTS)
    assert max(loads.values()) - min(loads.values()) <= 1
    assert {assignment["agent_type"] for assignment in plan["assignments"]} == {
        "prompt",
        "hosted_code",
        "hosted_custom_container",
    }
    for assignment in plan["assignments"]:
        agent = agents_by_id[assignment["agent_id"]]
        compatibility = scenarios[assignment["scenario_id"]]["compatibility"]
        assert agent["domain"] in compatibility["domains"]
        assert agent["agent_type"] in compatibility["agent_types"]


def test_runs_enforce_root_cause_limit_and_conflict_separation() -> None:
    _, catalog = _inputs()
    plan = generate_daily_plan(REPORT_DATE)
    scenarios = {scenario["id"]: scenario for scenario in catalog["scenarios"]}
    runs: dict[str, list[dict]] = defaultdict(list)
    for assignment in plan["assignments"]:
        runs[assignment["run_id"]].append(assignment)
    for assignments in runs.values():
        assert sum(item["expected"]["finding_count"] for item in assignments) <= 4
        assert len({item["agent_version_digest"] for item in assignments}) == 1
        assert len(
            {(item["window"]["start"], item["window"]["end"]) for item in assignments}
        ) == 1
        conflict_sets = [
            set(scenarios[item["scenario_id"]]["conflict_tags"]) for item in assignments
        ]
        for index, conflict_set in enumerate(conflict_sets):
            assert all(
                conflict_set.isdisjoint(other)
                for other in conflict_sets[index + 1 :]
            )


def test_sequential_lifecycle_gets_isolated_ordered_versions() -> None:
    plan = generate_daily_plan(REPORT_DATE)
    sequential = [
        assignment
        for assignment in plan["assignments"]
        if assignment["lifecycle"] == "sequential_faulted_and_corrected_versions"
    ]
    assert {item["scenario_id"] for item in sequential} == {
        "aiq-scn-058-cross-version-stale-finding",
        "aiq-scn-059-cross-window-dedup",
        "aiq-scn-060-fixed-issue-recurrence",
    }
    run_counts = Counter(item["run_id"] for item in plan["assignments"])
    for assignment in sequential:
        assert run_counts[assignment["run_id"]] == 1
        phases = [version["phase"] for version in assignment["version_sequence"]]
        if assignment["scenario_id"] == "aiq-scn-059-cross-window-dedup":
            assert phases == ["faulted-initial", "faulted-repeat"]
            assert len({version["digest"] for version in assignment["version_sequence"]}) == 1
        else:
            assert phases[:2] == ["faulted", "corrected"]
        if assignment["scenario_id"] == "aiq-scn-060-fixed-issue-recurrence":
            assert phases == ["faulted", "corrected", "recurred"]
        for version in assignment["version_sequence"]:
            assert version["window"]["start"].endswith(
                f"/{version['phase']}/start-inclusive"
            )
            assert version["window"]["end"].endswith(
                f"/{version['phase']}/end-exclusive"
            )

    repeated = next(
        item
        for item in sequential
        if item["scenario_id"] == "aiq-scn-059-cross-window-dedup"
    )
    invalid = deepcopy(plan)
    invalid_repeated = next(
        item
        for item in invalid["assignments"]
        if item["scenario_id"] == repeated["scenario_id"]
    )
    invalid_repeated["version_sequence"][1]["digest"] = "sha256:" + ("d" * 64)
    invalid["plan_digest"] = canonical_plan_digest(invalid)
    agents, catalog = _inputs()
    with pytest.raises(ContractError, match="repeated version key"):
        validate_daily_plan_semantics(invalid, agents, catalog, "plan")


def test_multi_root_collection_case_consumes_two_run_slots() -> None:
    plan = generate_daily_plan(REPORT_DATE)
    umbrella = next(
        item
        for item in plan["assignments"]
        if item["scenario_id"] == "aiq-scn-062-umbrella-insight"
    )
    run = [item for item in plan["assignments"] if item["run_id"] == umbrella["run_id"]]
    assert umbrella["expected"]["finding_count"] == 2
    assert sum(item["expected"]["finding_count"] for item in run) <= 4


def test_rerun_id_is_schema_valid_without_changing_seeded_assignments() -> None:
    original = generate_daily_plan(REPORT_DATE)
    rerun = generate_daily_plan(REPORT_DATE, rerun=1)
    assert rerun["plan_id"] == "aiq-20260821-r01"
    assert rerun["seed"] == original["seed"]
    assert rerun["assignments"] == original["assignments"]
    assert rerun["plan_digest"] != original["plan_digest"]

    invalid = deepcopy(rerun)
    invalid["project"]["name"] = original["plan_id"]
    invalid["plan_digest"] = canonical_plan_digest(invalid)
    agents, catalog = _inputs()
    with pytest.raises(ContractError, match="project name"):
        validate_daily_plan_semantics(invalid, agents, catalog, "plan")


def test_historical_plan_remains_internally_valid_after_catalog_version_change() -> None:
    agents, catalog = _inputs()
    plan = generate_daily_plan(REPORT_DATE, agents=agents, catalog=catalog)
    future_catalog = deepcopy(catalog)
    future_catalog["catalog_version"] = "2.0.0"
    with pytest.raises(ContractError, match="catalog_version"):
        validate_daily_plan_semantics(plan, agents, future_catalog, "plan")
    validate_daily_plan_semantics(
        plan,
        agents,
        future_catalog,
        "plan",
        allow_historical=True,
    )


def test_rendered_plan_has_assignment_wave_finding_and_control_tables() -> None:
    _, catalog = _inputs()
    plan = generate_daily_plan(REPORT_DATE, catalog=catalog)
    markdown = render_plan_markdown(plan, catalog)
    assert "## Assignments" in markdown
    assert "## Waves" in markdown
    assert "## Expected findings" in markdown
    assert "## Healthy and negative controls" in markdown
    assert plan["plan_digest"] in markdown
    assert "https://" not in markdown
    assert "$AIQ_DEPLOYED_AGENT_ENDPOINT" not in markdown


def test_write_daily_plan_persists_replayable_bytes(tmp_path) -> None:
    json_path, markdown_path = write_daily_plan(REPORT_DATE, tmp_path)
    expected = generate_daily_plan(REPORT_DATE)
    assert json_path.read_bytes() == serialize_plan(expected)
    assert expected["plan_id"] in markdown_path.read_text(encoding="ascii")
