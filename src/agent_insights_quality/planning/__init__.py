from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from agent_insights_quality.contracts import (
    ContractError,
    ROOT,
    catalog_bundle_hash,
    expected_finding_count,
    load_agent_manifests,
    load_scenario_catalog,
    load_selection_policy,
    mandatory_scenarios_for_weekday,
    selection_policy_hash,
    validate_daily_plan_semantics,
    validate_instance,
    validate_supporting_manifests,
)


PLANNER_VERSION = "2.0.0"
ENGINE_BUILD = "public-agent-insights-daily"
GENERATOR_MODEL = "gpt-5.6-terra"


def _sha256(value: str | bytes) -> str:
    content = value.encode("utf-8") if isinstance(value, str) else value
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def catalog_hash(path: Path | None = None) -> str:
    return catalog_bundle_hash(path)


def canonical_plan_digest(plan: dict[str, Any]) -> str:
    content = {key: value for key, value in plan.items() if key != "plan_digest"}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _sha256(encoded)


def _eligible_agents(
    scenario: dict[str, Any],
    agents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    compatibility = scenario["compatibility"]
    return [
        agent
        for agent in agents
        if agent["domain"] in compatibility["domains"]
        and agent["agent_type"] in compatibility["agent_types"]
        and (
            not compatibility["agent_ids"]
            or agent["id"] in compatibility["agent_ids"]
        )
    ]


def _assign_agents(
    scenarios: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    seed: int,
    *,
    expected_cap: int | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    agent_order = sorted(agents, key=lambda item: item["id"])
    priority = {"P0": 0, "P1": 1, "P2": 2}
    ordered = sorted(
        scenarios,
        key=lambda scenario: (
            expected_finding_count(scenario) == 0,
            len(_eligible_agents(scenario, agent_order)),
            -expected_finding_count(scenario),
            priority[scenario["priority"]],
            _sha256(f"{seed}:assignment-order:{scenario['id']}"),
        ),
    )
    total = len(ordered)
    maximum_count = (total + len(agent_order) - 1) // len(agent_order)
    minimum_count = total // len(agent_order)
    roots = [0] * len(agent_order)
    counts = [0] * len(agent_order)
    selected: list[int] = []
    failed: set[tuple[int, tuple[int, ...], tuple[int, ...]]] = set()
    indexes = {agent["id"]: index for index, agent in enumerate(agent_order)}

    def search(index: int) -> bool:
        if index == total:
            return min(counts) >= minimum_count and max(counts) <= maximum_count
        state = (index, tuple(roots), tuple(counts))
        if state in failed:
            return False
        remaining = total - index
        if sum(max(0, minimum_count - count) for count in counts) > remaining:
            failed.add(state)
            return False
        scenario = ordered[index]
        weight = expected_finding_count(scenario)
        eligible = [indexes[agent["id"]] for agent in _eligible_agents(scenario, agent_order)]
        eligible.sort(
            key=lambda agent_index: (
                roots[agent_index],
                counts[agent_index],
                _sha256(
                    f"{seed}:{scenario['id']}:{agent_order[agent_index]['id']}"
                ),
            )
        )
        for agent_index in eligible:
            if counts[agent_index] >= maximum_count:
                continue
            if expected_cap is not None and roots[agent_index] + weight > expected_cap:
                continue
            roots[agent_index] += weight
            counts[agent_index] += 1
            selected.append(agent_index)
            if search(index + 1):
                return True
            selected.pop()
            counts[agent_index] -= 1
            roots[agent_index] -= weight
        failed.add(state)
        return False

    if not search(0):
        raise ContractError(
            "Selected scenarios cannot fit compatible agents within the reviewed expected cap"
        )
    return [
        (scenario, agent_order[agent_index])
        for scenario, agent_index in zip(ordered, selected, strict=True)
    ]


def _cycle_metadata(
    report_date: date,
    policy: dict[str, Any],
    catalog_digest: str,
    policy_digest: str,
) -> tuple[dict[str, Any], int]:
    epoch = date.fromisoformat(policy["cycle"]["epoch_monday"])
    cycle_number = (report_date - epoch).days // 7
    weekday_index = report_date.weekday()
    cycle_index = weekday_index if weekday_index < 5 else -1
    cycle_id = (
        f"cycle-{cycle_number}-"
        + hashlib.sha256(
            f"{catalog_digest}:{policy_digest}:{cycle_number}".encode("ascii")
        ).hexdigest()[:12]
    )
    return (
        {
            "id": cycle_id,
            "number": cycle_number,
            "business_day": cycle_index + 1 if cycle_index >= 0 else None,
            "weekday": (
                policy["cycle"]["weekdays"][cycle_index]
                if cycle_index >= 0
                else "non_scheduled"
            ),
            "length_business_days": policy["cycle"]["business_days"],
            "full_coverage_horizon_business_days": policy["cycle"][
                "business_days"
            ],
        },
        cycle_index,
    )


def _partition_candidate(
    rotating: list[dict[str, Any]],
    sizes: list[int],
    *,
    catalog_digest: str,
    policy_digest: str,
    cycle_number: int,
    nonce: int,
    rotating_root_caps: list[int],
) -> list[list[dict[str, Any]]] | None:
    partitions: list[list[dict[str, Any]]] = [[] for _ in sizes]
    roots = [0] * len(sizes)
    categories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for scenario in rotating:
        categories[scenario["expected"]["category"]].append(scenario)
    priority = {"P0": 0, "P1": 1, "P2": 2}
    category_order = sorted(categories, key=lambda name: (len(categories[name]), name))
    for category in category_order:
        ordered = sorted(
            categories[category],
            key=lambda scenario: (
                priority[scenario["priority"]],
                _sha256(
                    f"{catalog_digest}:{policy_digest}:{cycle_number}:{nonce}:"
                    f"{scenario['id']}"
                ),
            ),
        )
        for scenario in ordered:
            weight = expected_finding_count(scenario)
            eligible_days = [
                day
                for day, capacity in enumerate(sizes)
                if len(partitions[day]) < capacity
                and roots[day] + weight <= rotating_root_caps[day]
            ]
            if not eligible_days:
                return None
            eligible_days.sort(
                key=lambda day: (
                    any(
                        item["expected"]["category"] == category
                        for item in partitions[day]
                    ),
                    len(partitions[day]) / sizes[day],
                    roots[day],
                    _sha256(
                        f"{catalog_digest}:{cycle_number}:{nonce}:{scenario['id']}:{day}"
                    ),
                )
            )
            day = eligible_days[0]
            partitions[day].append(scenario)
            roots[day] += weight
    if [len(partition) for partition in partitions] != sizes:
        return None
    return partitions


def rotating_cycle_partitions(
    catalog: dict[str, Any],
    agents: list[dict[str, Any]],
    policy: dict[str, Any],
    catalog_digest: str,
    policy_digest: str,
    cycle_number: int,
) -> list[list[dict[str, Any]]]:
    active = [scenario for scenario in catalog["scenarios"] if scenario["status"] == "active"]
    daily_mandatory = [
        mandatory_scenarios_for_weekday(active, policy, weekday)
        for weekday in policy["cycle"]["weekdays"]
    ]
    rotating = [
        scenario
        for scenario in active
        if scenario["expected"]["category"] != "none"
        and scenario["priority"] in policy["selection"]["rotating_fault_priorities"]
    ]
    sizes = policy["cycle"]["partition_scenario_counts"]
    rotating_root_caps = [
        policy["limits"]["daily_expected_root_count"]
        - sum(expected_finding_count(scenario) for scenario in mandatory)
        for mandatory in daily_mandatory
    ]
    best: tuple[tuple[int, int], list[list[dict[str, Any]]]] | None = None
    required_categories = set(policy["selection"]["required_fault_categories"])
    for nonce in range(256):
        partitions = _partition_candidate(
            rotating,
            sizes,
            catalog_digest=catalog_digest,
            policy_digest=policy_digest,
            cycle_number=cycle_number,
            nonce=nonce,
            rotating_root_caps=rotating_root_caps,
        )
        if partitions is None:
            continue
        try:
            for day, partition in enumerate(partitions):
                _assign_agents(
                    daily_mandatory[day] + partition,
                    agents,
                    int(
                        hashlib.sha256(
                            f"{catalog_digest}:{policy_digest}:{cycle_number}:{day}".encode(
                                "ascii"
                            )
                        ).hexdigest()[:16],
                        16,
                    ),
                    expected_cap=policy["limits"]["expected_insight_cap_per_agent"],
                )
        except ContractError:
            continue
        coverage = [
            len(
                {
                    scenario["expected"]["category"]
                    for scenario in daily_mandatory[day] + partition
                    if scenario["expected"]["category"] in required_categories
                }
            )
            for day, partition in enumerate(partitions)
        ]
        score = (min(coverage), sum(coverage))
        if best is None or score > best[0]:
            best = (score, partitions)
    if best is None:
        raise ContractError(
            "The rotating catalog cannot be partitioned into five assignable weekday selections"
        )
    return best[1]


def _select_scenarios(
    report_date: date,
    catalog: dict[str, Any],
    agents: list[dict[str, Any]],
    policy: dict[str, Any],
    catalog_digest: str,
    policy_digest: str,
    *,
    full_catalog: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    active = [scenario for scenario in catalog["scenarios"] if scenario["status"] == "active"]
    cycle, cycle_index = _cycle_metadata(
        report_date,
        policy,
        catalog_digest,
        policy_digest,
    )
    if full_catalog:
        reasons = {
            scenario["id"]: "full_catalog_release"
            for scenario in active
        }
        selection = {
            "cycle": cycle,
            "mandatory_scenario_ids": [],
            "rotating_scenario_ids": [],
            "selected_scenario_ids": [],
            "omitted_scenario_ids": [],
            "selection_reasons": reasons,
        }
        return active, selection, reasons
    if cycle_index < 0:
        raise ContractError(
            "Rotating daily plans are scheduled Monday through Friday; "
            "use --full-catalog only for explicit release qualification."
        )
    weekday = policy["cycle"]["weekdays"][cycle_index]
    mandatory = mandatory_scenarios_for_weekday(active, policy, weekday)
    partitions = rotating_cycle_partitions(
        catalog,
        agents,
        policy,
        catalog_digest,
        policy_digest,
        cycle["number"],
    )
    rotating = partitions[cycle_index]
    selected = mandatory + rotating
    reasons = {
        scenario["id"]: (
            "healthy_control_daily"
            if scenario["expected"]["category"] == "none"
            else (
                "p0_collection_probe_cadence"
                if scenario["id"] in policy["selection"]["scenario_cadence"]
                else "p0_fault_daily"
            )
        )
        for scenario in mandatory
    }
    reasons.update(
        {scenario["id"]: "rotating_priority_fairness" for scenario in rotating}
    )
    selection = {
        "cycle": cycle,
        "mandatory_scenario_ids": [],
        "rotating_scenario_ids": [],
        "selected_scenario_ids": [],
        "omitted_scenario_ids": sorted(
            {scenario["id"] for scenario in active}
            - {scenario["id"] for scenario in selected}
        ),
        "selection_reasons": reasons,
    }
    return selected, selection, reasons


def _group_runs(
    assigned: list[tuple[dict[str, Any], dict[str, Any]]],
    root_cap: int,
) -> list[dict[str, Any]]:
    per_agent: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for scenario, agent in assigned:
        per_agent[agent["id"]].append((scenario, agent))

    runs = []
    for agent_id in sorted(per_agent):
        buckets: list[dict[str, Any]] = []
        ordered = sorted(
            per_agent[agent_id],
            key=lambda item: (
                item[0]["expected"]["category"] != "none",
                item[0]["id"],
            ),
        )
        for scenario, agent in ordered:
            sequential = (
                scenario["version_semantics"]["applies_to"]
                == "sequential_faulted_and_corrected_versions"
            )
            selected = None
            if not sequential:
                for bucket in buckets:
                    conflicts = set(scenario["conflict_tags"])
                    fault_count = sum(
                        expected_finding_count(item)
                        for item, _ in bucket["items"]
                    )
                    if (
                        not bucket["exclusive"]
                        and bucket["phase"] == scenario["version_semantics"]["phases"][0]
                        and fault_count + expected_finding_count(scenario) <= root_cap
                        and conflicts.isdisjoint(bucket["conflict_tags"])
                    ):
                        selected = bucket
                        break
            if selected is None:
                selected = {
                    "agent": agent,
                    "items": [],
                    "conflict_tags": set(),
                    "exclusive": sequential,
                    "phase": scenario["version_semantics"]["phases"][0],
                }
                buckets.append(selected)
            selected["items"].append((scenario, agent))
            selected["conflict_tags"].update(scenario["conflict_tags"])

        for wave, bucket in enumerate(buckets):
            bucket["wave"] = wave
            bucket["run_id"] = f"run-{wave:02d}-{agent_id}"
            runs.append(bucket)
    return runs


def _window(run_id: str, phase: str) -> dict[str, str]:
    return {
        "start": f"window://{run_id}/{phase}/start-inclusive",
        "end": f"window://{run_id}/{phase}/end-exclusive",
    }


def _version_sequence(
    scenario: dict[str, Any],
    run_id: str,
    seed: int,
    bucket_scenarios: list[str],
) -> list[dict[str, Any]]:
    phases = scenario["version_semantics"]["phases"]
    version_keys = scenario["version_semantics"]["version_keys"]
    sequence = []
    for phase, version_key in zip(phases, version_keys, strict=True):
        digest_input = (
            f"{seed}:{run_id}:{version_key}:"
            + ",".join(bucket_scenarios if len(phases) == 1 else [scenario["id"]])
        )
        sequence.append(
            {
                "phase": phase,
                "version_key": version_key,
                "digest": _sha256(digest_input),
                "window": _window(run_id, phase),
            }
        )
    return sequence


def generate_daily_plan(
    report_date: date,
    *,
    catalog: dict[str, Any] | None = None,
    agents: list[dict[str, Any]] | None = None,
    policy: dict[str, Any] | None = None,
    catalog_digest: str | None = None,
    rerun: int = 0,
    full_catalog: bool = False,
) -> dict[str, Any]:
    if rerun < 0 or rerun > 99:
        raise ValueError("rerun must be between 0 and 99")
    agents = agents if agents is not None else load_agent_manifests()
    catalog = (
        catalog
        if catalog is not None
        else load_scenario_catalog({agent["id"] for agent in agents})
    )
    policy = policy if policy is not None else load_selection_policy(catalog)
    validate_supporting_manifests(catalog)
    computed_digest = catalog_bundle_hash(catalog=catalog, agents=agents)
    if catalog_digest is not None and catalog_digest != computed_digest:
        raise ContractError("catalog_digest does not match the supplied planning bundle")
    digest = computed_digest
    policy_digest = selection_policy_hash(policy)
    seed = int(
        hashlib.sha256(
            f"{report_date.isoformat()}:{digest}:{policy_digest}".encode("ascii")
        ).hexdigest()[:16],
        16,
    )
    selected, selection, reasons = _select_scenarios(
        report_date,
        catalog,
        agents,
        policy,
        digest,
        policy_digest,
        full_catalog=full_catalog,
    )
    expected_cap = (
        None
        if full_catalog
        else policy["limits"]["expected_insight_cap_per_agent"]
    )
    assigned = _assign_agents(selected, agents, seed, expected_cap=expected_cap)
    runs = _group_runs(assigned, policy["limits"]["expected_root_cap_per_run"])
    rerun_name_suffix = f"-r{rerun:02d}" if rerun else ""

    assignments = []
    for run in runs:
        bucket_scenarios = sorted(scenario["id"] for scenario, _ in run["items"])
        for scenario, agent in run["items"]:
            sequence = _version_sequence(
                scenario,
                run["run_id"],
                seed,
                bucket_scenarios,
            )
            agent_name = (
                f"{agent['required_name_prefix']}-{report_date:%Y%m%d}"
                f"{rerun_name_suffix}-w{run['wave']:02d}"
            )
            if len(agent_name) > 63:
                raise ContractError("Generated agent name exceeds the service limit")
            assignments.append(
                {
                    "scenario_id": scenario["id"],
                    "scenario_version": scenario["version"],
                    "family": scenario["family"],
                    "selection_reason": reasons[scenario["id"]],
                    "conflict_tags": scenario["conflict_tags"],
                    "run_id": run["run_id"],
                    "agent_id": agent["id"],
                    "agent_name": agent_name,
                    "agent_type": agent["agent_type"],
                    "agent_version_digest": sequence[0]["digest"],
                    "version_sequence": sequence,
                    "wave": run["wave"],
                    "traffic_seed": int(
                        hashlib.sha256(
                            f"{seed}:{scenario['traffic']['seed_namespace']}".encode("ascii")
                        ).hexdigest()[:16],
                        16,
                    ),
                    "traffic_seed_namespace": scenario["traffic"]["seed_namespace"],
                    "traffic_recipe_id": scenario["traffic"]["recipe_id"],
                    "traffic_requests": scenario["traffic"]["minimum_requests"],
                    "lifecycle": scenario["version_semantics"]["applies_to"],
                    "window": sequence[0]["window"],
                    "expected": {
                        "category": scenario["expected"]["category"],
                        "severity": scenario["expected"]["severity"],
                        "finding_count": expected_finding_count(scenario),
                        "validation_targets": scenario["expected"]["validation_targets"],
                    },
                }
            )
    assignments.sort(key=lambda item: (item["wave"], item["agent_id"], item["scenario_id"]))
    selected_ids = [assignment["scenario_id"] for assignment in assignments]
    selection["selected_scenario_ids"] = selected_ids
    selection["mandatory_scenario_ids"] = [
        scenario_id
        for scenario_id in selected_ids
        if reasons[scenario_id]
        in {
            "healthy_control_daily",
            "p0_fault_daily",
            "p0_collection_probe_cadence",
        }
    ]
    selection["rotating_scenario_ids"] = [
        scenario_id
        for scenario_id in selected_ids
        if reasons[scenario_id] == "rotating_priority_fairness"
    ]
    plan_id = f"aiq-{report_date:%Y%m%d}" + (f"-r{rerun:02d}" if rerun else "")
    artifact_directory = (
        f"reports/daily/{report_date:%Y/%m/%d}"
        + (f"/{plan_id}" if rerun else "")
    )
    selected_by_id = {scenario["id"]: scenario for scenario in selected}
    per_agent_expected = {
        agent["id"]: sum(
            assignment["expected"]["finding_count"]
            for assignment in assignments
            if assignment["agent_id"] == agent["id"]
        )
        for agent in sorted(agents, key=lambda item: item["id"])
    }
    plan = {
        "schema_version": "1.0.0",
        "plan_id": plan_id,
        "plan_digest": "",
        "artifact_directory": artifact_directory,
        "report_date": report_date.isoformat(),
        "created_at": f"{report_date.isoformat()}T00:00:00Z",
        "catalog_version": catalog["catalog_version"],
        "catalog_hash": digest,
        "policy_version": policy["policy_version"],
        "policy_hash": policy_digest,
        "planner_version": PLANNER_VERSION,
        "seed": seed,
        "selection_mode": "full_catalog" if full_catalog else "rotating_daily",
        "human_daily_contract": not full_catalog,
        "selection": selection,
        "limits": {
            "expected_insight_cap_per_agent": policy["limits"][
                "expected_insight_cap_per_agent"
            ],
            "expected_root_cap_per_run": policy["limits"]["expected_root_cap_per_run"],
            "actual_insight_count_rule": policy["limits"][
                "actual_insight_count_rule"
            ],
            "expected_cap_enforced": not full_catalog,
        },
        "per_agent_expected_totals": per_agent_expected,
        "engine": {
            "endpoint_reference": _sha256("runtime:agent-insights-endpoint"),
            "build": ENGINE_BUILD,
            "generator_model": GENERATOR_MODEL,
        },
        "project": {
            "name": plan_id,
            "resource_reference": _sha256(f"runtime:project:{plan_id}"),
            "expires_on": (report_date + timedelta(days=7)).isoformat(),
        },
        "coverage": {
            "scenario_count": len(assignments),
            "healthy_control_count": sum(
                item["expected"]["finding_count"] == 0 for item in assignments
            ),
            "families": sorted(
                {selected_by_id[scenario_id]["family"] for scenario_id in selected_ids}
            ),
            "categories": sorted(
                {
                    selected_by_id[scenario_id]["expected"]["category"]
                    for scenario_id in selected_ids
                }
            ),
            "severities": sorted(
                {
                    selected_by_id[scenario_id]["expected"]["severity"]
                    for scenario_id in selected_ids
                }
            ),
            "agent_types": sorted({item["agent_type"] for item in assignments}),
        },
        "assignments": assignments,
    }
    plan["plan_digest"] = canonical_plan_digest(plan)
    validate_instance(plan, ROOT / "schemas" / "daily-plan.schema.json", "generated plan")
    validate_daily_plan_semantics(
        plan,
        agents,
        catalog,
        "generated plan",
        expected_catalog_hash=digest,
        expected_policy=policy,
    )
    return plan


def serialize_plan(plan: dict[str, Any]) -> bytes:
    return (json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def render_plan_markdown(
    plan: dict[str, Any],
    catalog: dict[str, Any],
) -> str:
    scenarios = {scenario["id"]: scenario for scenario in catalog["scenarios"]}
    cycle = plan["selection"]["cycle"]
    lines = [
        "# Daily Agent Insights Quality Plan",
        "",
        "<!-- Generated by `python -m agent_insights_quality plan`; do not edit. -->",
        "",
        f"- Plan: `{plan['plan_id']}`",
        f"- Plan digest: `{plan['plan_digest']}`",
        f"- Artifact directory: `{plan['artifact_directory']}`",
        f"- Report date: `{plan['report_date']}`",
        f"- Catalog: `{plan['catalog_version']}` (`{plan['catalog_hash']}`)",
        f"- Selection policy: `{plan['policy_version']}` (`{plan['policy_hash']}`)",
        f"- Selection mode: `{plan['selection_mode']}`",
        f"- Human daily contract: `{str(plan['human_daily_contract']).lower()}`",
        f"- Cycle: `{cycle['id']}`, {cycle['weekday']} "
        f"(business day {cycle['business_day'] or 'N/A'} of {cycle['length_business_days']})",
        "- Full-coverage horizon: "
        f"{cycle['full_coverage_horizon_business_days']} business days",
        f"- Deterministic seed: `{plan['seed']}`",
        "",
        "## Selection",
        "",
        f"- Mandatory scenarios: {len(plan['selection']['mandatory_scenario_ids'])}",
        f"- Rotating scenarios: {len(plan['selection']['rotating_scenario_ids'])}",
        f"- Selected scenarios: {len(plan['selection']['selected_scenario_ids'])}",
        f"- Omitted scenarios: {len(plan['selection']['omitted_scenario_ids'])}",
        "",
        "Selected: "
        + ", ".join(f"`{value}`" for value in plan["selection"]["selected_scenario_ids"]),
        "",
        "Omitted: "
        + (
            ", ".join(f"`{value}`" for value in plan["selection"]["omitted_scenario_ids"])
            or "none"
        ),
        "",
        "## Per-agent expected totals",
        "",
        "| Agent | Expected roots | Expected cap | Actual count rule |",
        "| --- | ---: | ---: | --- |",
    ]
    for agent_id, total in plan["per_agent_expected_totals"].items():
        lines.append(
            f"| `{agent_id}` | {total} | "
            f"{plan['limits']['expected_insight_cap_per_agent']} | "
            f"`{plan['limits']['actual_insight_count_rule']}` |"
        )
    lines.extend(
        [
            "",
            "## Assignments",
            "",
            "| Wave | Run | Agent | Type | Scenario | Reason | Version | Requests | Lifecycle |",
            "| ---: | --- | --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for item in plan["assignments"]:
        scenario = scenarios[item["scenario_id"]]
        lines.append(
            f"| {item['wave']} | `{item['run_id']}` | `{item['agent_id']}` | "
            f"`{item['agent_type']}` | `{item['scenario_id']}` - {scenario['title']} | "
            f"`{item['selection_reason']}` | `{item['scenario_version']}` | "
            f"{item['traffic_requests']} | `{item['lifecycle']}` |"
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in plan["assignments"]:
        grouped[item["run_id"]].append(item)
    lines.extend(
        [
            "",
            "## Waves",
            "",
            "| Run | Wave | Agent version | Root causes | Half-open window |",
            "| --- | ---: | --- | ---: | --- |",
        ]
    )
    for run_id in sorted(grouped):
        items = grouped[run_id]
        root_causes = sum(item["expected"]["finding_count"] for item in items)
        window = items[0]["window"]
        lines.append(
            f"| `{run_id}` | {items[0]['wave']} | `{items[0]['agent_version_digest']}` | "
            f"{root_causes} | `{window['start']}` to `{window['end']}` |"
        )

    lines.extend(
        [
            "",
            "## Expected findings",
            "",
            "| Scenario | Category | Severity | Validation targets |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in plan["assignments"]:
        if item["expected"]["finding_count"] == 0:
            continue
        targets = ", ".join(f"`{target}`" for target in item["expected"]["validation_targets"])
        lines.append(
            f"| `{item['scenario_id']}` | `{item['expected']['category']}` | "
            f"`{item['expected']['severity']}` | {targets} |"
        )

    lines.extend(
        [
            "",
            "## Healthy and negative controls",
            "",
            "| Scenario | Agent | Expected insight count | Healthy decoys |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for item in plan["assignments"]:
        scenario = scenarios[item["scenario_id"]]
        if item["expected"]["finding_count"] != 0:
            continue
        decoys = "; ".join(scenario["healthy_decoys"])
        lines.append(
            f"| `{item['scenario_id']}` | `{item['agent_id']}` | 0 | {decoys} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_daily_plan(
    report_date: date,
    output_dir: Path | None = None,
    *,
    rerun: int = 0,
    full_catalog: bool = False,
) -> tuple[Path, Path]:
    agents = load_agent_manifests()
    catalog = load_scenario_catalog({agent["id"] for agent in agents})
    plan = generate_daily_plan(
        report_date,
        catalog=catalog,
        agents=agents,
        rerun=rerun,
        full_catalog=full_catalog,
    )
    if output_dir is None:
        destination = ROOT / Path(plan["artifact_directory"])
    else:
        destination = output_dir / plan["plan_id"] if rerun else output_dir
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "plan.json"
    markdown_path = destination / "plan.md"
    json_path.write_bytes(serialize_plan(plan))
    markdown_path.write_bytes(render_plan_markdown(plan, catalog).encode("ascii"))
    return json_path, markdown_path
