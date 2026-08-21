from __future__ import annotations

import hashlib
import json
import random
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
    validate_daily_plan_semantics,
    validate_instance,
    validate_supporting_manifests,
)


PLANNER_VERSION = "1.0.0"
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
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    ordered = list(scenarios)
    random.Random(seed).shuffle(ordered)
    ordered.sort(key=lambda scenario: len(_eligible_agents(scenario, agents)))
    loads = {agent["id"]: 0 for agent in agents}
    assignments = []
    for scenario in ordered:
        eligible = _eligible_agents(scenario, agents)
        agent = min(
            eligible,
            key=lambda item: (
                loads[item["id"]],
                _sha256(f"{seed}:{scenario['id']}:{item['id']}"),
            ),
        )
        loads[agent["id"]] += 1
        assignments.append((scenario, agent))
    return assignments


def _group_runs(
    assigned: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    per_agent: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for scenario, agent in assigned:
        per_agent[agent["id"]].append((scenario, agent))

    runs = []
    for agent_id in sorted(per_agent):
        buckets: list[dict[str, Any]] = []
        for scenario, agent in per_agent[agent_id]:
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
                        and fault_count + expected_finding_count(scenario) <= 4
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
    catalog_digest: str | None = None,
    rerun: int = 0,
) -> dict[str, Any]:
    if rerun < 0 or rerun > 99:
        raise ValueError("rerun must be between 0 and 99")
    agents = agents if agents is not None else load_agent_manifests()
    catalog = (
        catalog
        if catalog is not None
        else load_scenario_catalog({agent["id"] for agent in agents})
    )
    validate_supporting_manifests(catalog)
    computed_digest = catalog_bundle_hash(catalog=catalog, agents=agents)
    if catalog_digest is not None and catalog_digest != computed_digest:
        raise ContractError("catalog_digest does not match the supplied planning bundle")
    digest = computed_digest
    seed = int(
        hashlib.sha256(f"{report_date.isoformat()}:{digest}".encode("ascii")).hexdigest()[:16],
        16,
    )
    active = [scenario for scenario in catalog["scenarios"] if scenario["status"] == "active"]
    assigned = _assign_agents(active, agents, seed)
    runs = _group_runs(assigned)

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
            assignments.append(
                {
                    "scenario_id": scenario["id"],
                    "scenario_version": scenario["version"],
                    "family": scenario["family"],
                    "conflict_tags": scenario["conflict_tags"],
                    "run_id": run["run_id"],
                    "agent_id": agent["id"],
                    "agent_name": (
                        f"{agent['required_name_prefix']}-{report_date:%Y%m%d}-"
                        f"w{run['wave']:02d}"
                    ),
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
    plan_id = f"aiq-{report_date:%Y%m%d}" + (f"-r{rerun:02d}" if rerun else "")
    artifact_directory = (
        f"reports/daily/{report_date:%Y/%m/%d}"
        + (f"/{plan_id}" if rerun else "")
    )
    plan = {
        "schema_version": "1.0.0",
        "plan_id": plan_id,
        "plan_digest": "",
        "artifact_directory": artifact_directory,
        "report_date": report_date.isoformat(),
        "created_at": f"{report_date.isoformat()}T00:00:00Z",
        "catalog_version": catalog["catalog_version"],
        "catalog_hash": digest,
        "planner_version": PLANNER_VERSION,
        "seed": seed,
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
            "families": sorted({scenario["family"] for scenario in active}),
            "categories": sorted({scenario["expected"]["category"] for scenario in active}),
            "severities": sorted({scenario["expected"]["severity"] for scenario in active}),
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
    )
    return plan


def serialize_plan(plan: dict[str, Any]) -> bytes:
    return (json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def render_plan_markdown(
    plan: dict[str, Any],
    catalog: dict[str, Any],
) -> str:
    scenarios = {scenario["id"]: scenario for scenario in catalog["scenarios"]}
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
        f"- Deterministic seed: `{plan['seed']}`",
        "",
        "## Assignments",
        "",
        "| Wave | Run | Agent | Type | Scenario | Version | Requests | Lifecycle |",
        "| ---: | --- | --- | --- | --- | --- | ---: | --- |",
    ]
    for item in plan["assignments"]:
        scenario = scenarios[item["scenario_id"]]
        lines.append(
            f"| {item['wave']} | `{item['run_id']}` | `{item['agent_id']}` | "
            f"`{item['agent_type']}` | `{item['scenario_id']}` - {scenario['title']} | "
            f"`{item['scenario_version']}` | {item['traffic_requests']} | `{item['lifecycle']}` |"
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
) -> tuple[Path, Path]:
    agents = load_agent_manifests()
    catalog = load_scenario_catalog({agent["id"] for agent in agents})
    plan = generate_daily_plan(report_date, catalog=catalog, agents=agents, rerun=rerun)
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
