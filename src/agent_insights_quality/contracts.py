from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.source_artifacts import reviewed_source_files


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
AGENT_SCHEMA = SCHEMAS / "agent-manifest.schema.json"
SCENARIO_SCHEMA = SCHEMAS / "scenario-manifest.schema.json"
MEMORY_SCHEMA = SCHEMAS / "quality-memory.schema.json"
SCORECARD_SCHEMA = SCHEMAS / "scorecard.schema.json"
READINESS_FAILURE_SCHEMA = SCHEMAS / "readiness-failure.schema.json"
EMAIL_HANDOFF_SCHEMA = SCHEMAS / "email-handoff.schema.json"
SELECTION_POLICY_SCHEMA = SCHEMAS / "selection-policy.schema.json"
TRUST_FAILURES = {
    "structural_failure",
    "provenance_failure",
    "secret_or_pii",
    "judge_schema_failure",
    "unresolved_judgment",
}

EXPECTED_AGENTS = {
    "aiq-001-weather": "prompt",
    "aiq-002-healthcare": "prompt",
    "aiq-003-finance": "hosted_code",
    "aiq-004-travel": "hosted_code",
    "aiq-005-ticket": "hosted_custom_container",
}
EXPECTED_DEPLOYMENT_PROTOCOLS = {
    "prompt": "foundry_prompt_version_rest",
    "hosted_code": "foundry_hosted_multipart",
    "hosted_custom_container": "foundry_hosted_container_json",
}
REQUIRED_SCENARIO_FAMILIES = {
    "healthy_controls",
    "grounding_correctness",
    "system_instruction",
    "tool_selection",
    "tool_arguments",
    "tool_result_handling",
    "recovery",
    "planning",
    "capability_awareness",
    "context_memory",
    "context_cost",
    "response_completion",
    "safety_authorization",
    "latency_loops",
    "runtime_reliability",
    "multi_agent",
    "trace_interpretation",
    "insight_lifecycle",
    "collection_quality",
}
REQUIRED_CATEGORIES = {
    "tool_call_failures",
    "latency",
    "cost_tokens",
    "reliability_errors",
    "hallucinations",
    "output_quality",
    "context_memory",
    "safety_guardrails",
    "none",
}
REQUIRED_SEVERITIES = {"high", "medium", "low", "none"}
MINIMUM_ACTIVE_SCENARIOS = 63
MINIMUM_HEALTHY_CONTROLS = 4


class ContractError(ValueError):
    """Raised when a repository contract is invalid."""


def load_data(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix == ".json":
            return json.load(handle)
        return yaml.safe_load(handle)


def expected_finding_count(scenario: dict[str, Any]) -> int:
    return scenario["expected"].get(
        "finding_count",
        int(scenario["expected"]["category"] != "none"),
    )


def validate_instance(instance: Any, schema_path: Path, label: str) -> None:
    schema = load_data(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.absolute_path))
    if errors:
        details = []
        for error in errors:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            details.append(f"{label}:{location}: {error.message}")
        raise ContractError("\n".join(details))


def validate_schemas() -> None:
    schema_paths = sorted(SCHEMAS.glob("*.schema.json"))
    if not schema_paths:
        raise ContractError("No JSON schemas found")
    for path in schema_paths:
        schema = load_data(path)
        try:
            Draft202012Validator.check_schema(schema)
            for reference in _internal_schema_references(schema):
                _resolve_internal_schema_reference(schema, reference)
        except Exception as error:
            raise ContractError(f"{path.relative_to(ROOT)}: invalid schema: {error}") from error
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ContractError(f"{path.relative_to(ROOT)}: must use JSON Schema draft 2020-12")
        schema_id = schema.get("$id", "")
        expected_id = (
            "https://ninghu.github.io/agent-insights-quality/schemas/v1/"
            f"{path.name}"
        )
        if schema_id != expected_id:
            raise ContractError(
                f"{path.relative_to(ROOT)}: $id must be {expected_id}"
            )


def _internal_schema_references(value: Any) -> list[str]:
    references = []
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            references.append(reference)
        for child in value.values():
            references.extend(_internal_schema_references(child))
    elif isinstance(value, list):
        for child in value:
            references.extend(_internal_schema_references(child))
    return references


def _resolve_internal_schema_reference(schema: dict[str, Any], reference: str) -> Any:
    current: Any = schema
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise ContractError(f"unresolved internal schema reference: {reference}")
        current = current[part]
    return current


def validate_structured_file_syntax() -> None:
    roots = [
        ROOT / ".github",
        ROOT / "agents",
        ROOT / "config",
        ROOT / "reports",
        ROOT / "scenarios",
        ROOT / "schemas",
        ROOT / "state",
    ]
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}:
                try:
                    load_data(path)
                except (json.JSONDecodeError, yaml.YAMLError) as error:
                    raise ContractError(f"{path.relative_to(ROOT)}: invalid structured data: {error}") from error


def load_agent_manifests() -> list[dict[str, Any]]:
    manifests = []
    for path in sorted((ROOT / "agents").glob("*/manifest.yaml")):
        data = load_data(path)
        validate_instance(data, AGENT_SCHEMA, str(path.relative_to(ROOT)))
        if data["id"] != data["required_name_prefix"]:
            raise ContractError(f"{path.relative_to(ROOT)}: id and required_name_prefix must match")
        expected_source = path.parent.relative_to(ROOT).as_posix()
        if data["implementation"]["source_path"] != expected_source:
            raise ContractError(f"{path.relative_to(ROOT)}: implementation.source_path must be {expected_source}")
        expected_protocol = EXPECTED_DEPLOYMENT_PROTOCOLS[data["agent_type"]]
        if data["implementation"]["deployment_protocol"] != expected_protocol:
            raise ContractError(
                f"{path.relative_to(ROOT)}: {data['agent_type']} requires {expected_protocol}"
            )
        manifests.append(data)

    ids = [manifest["id"] for manifest in manifests]
    prefixes = [manifest["required_name_prefix"] for manifest in manifests]
    if len(ids) != len(set(ids)):
        raise ContractError("Agent IDs must be unique")
    if len(prefixes) != len(set(prefixes)):
        raise ContractError("Agent required name prefixes must be unique")
    actual = {manifest["id"]: manifest["agent_type"] for manifest in manifests}
    if actual != EXPECTED_AGENTS:
        raise ContractError(f"Initial agent registry must be exactly {EXPECTED_AGENTS}; found {actual}")
    return manifests


def load_scenario_catalog(agent_ids: set[str] | None = None) -> dict[str, Any]:
    path = ROOT / "scenarios" / "catalog.yaml"
    catalog = load_data(path)
    validate_instance(catalog, SCENARIO_SCHEMA, str(path.relative_to(ROOT)))
    agents = load_agent_manifests() if agent_ids is not None else []
    validate_scenario_catalog_semantics(catalog, agents)
    if agent_ids is not None:
        for scenario in catalog["scenarios"]:
            explicit_agents = set(scenario["compatibility"]["agent_ids"])
            if not explicit_agents.issubset(agent_ids):
                raise ContractError(f"{scenario['id']}: compatibility references unknown agents")
    return catalog


def validate_scenario_catalog_semantics(
    catalog: dict[str, Any],
    agents: list[dict[str, Any]] | None = None,
) -> None:
    scenario_ids = [scenario["id"] for scenario in catalog["scenarios"]]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ContractError("Scenario IDs must be unique")

    agents = agents or []
    for scenario in catalog["scenarios"]:
        mutation = scenario["mutation"]["manifest"]
        mutation_recipe = scenario["mutation"]["recipe_id"]
        if scenario["mutation"]["kind"] == "none" and (
            mutation is not None or mutation_recipe is not None
        ):
            raise ContractError(
                f"{scenario['id']}: a no-mutation scenario must not name a mutation recipe"
            )
        if scenario["mutation"]["kind"] != "none" and (
            mutation is None or mutation_recipe is None
        ):
            raise ContractError(f"{scenario['id']}: a fault scenario must name a mutation recipe")
        for relative in filter(None, [mutation, scenario["traffic"]["recipe"]]):
            if not (ROOT / relative).is_file():
                raise ContractError(f"{scenario['id']}: referenced file does not exist: {relative}")
        explicit_agents = set(scenario["compatibility"]["agent_ids"])
        if agents and not any(
            agent["domain"] in scenario["compatibility"]["domains"]
            and agent["agent_type"] in scenario["compatibility"]["agent_types"]
            and (not explicit_agents or agent["id"] in explicit_agents)
            for agent in agents
        ):
            raise ContractError(f"{scenario['id']}: compatibility cannot match a registered agent")
        category = scenario["expected"]["category"]
        severity = scenario["expected"]["severity"]
        if (category == "none") != (severity == "none"):
            raise ContractError(f"{scenario['id']}: category and severity none values must agree")
        finding_count = expected_finding_count(scenario)
        if (category == "none" and finding_count != 0) or (
            category != "none" and finding_count < 1
        ):
            raise ContractError(f"{scenario['id']}: finding count does not match expected category")
        if (category == "none") != ("no_insight" in scenario["expected"]["validation_targets"]):
            raise ContractError(f"{scenario['id']}: no_insight target must match a healthy control")
        if not scenario["conflict_tags"]:
            raise ContractError(f"{scenario['id']}: at least one conflict tag is required")
        if not scenario["evidence"]["negative_controls"]:
            raise ContractError(f"{scenario['id']}: at least one negative evidence control is required")
        semantics = scenario["version_semantics"]
        phases = semantics["phases"]
        version_keys = semantics["version_keys"]
        if len(phases) != len(version_keys):
            raise ContractError(f"{scenario['id']}: lifecycle phases and version keys must align")
        if semantics["applies_to"] == "current_immutable_version" and phases != ["healthy"]:
            raise ContractError(f"{scenario['id']}: current-version controls require a healthy phase")
        if semantics["applies_to"] == "injected_immutable_version" and phases != ["faulted"]:
            raise ContractError(f"{scenario['id']}: injected versions require one faulted phase")
        if (
            semantics["applies_to"] == "sequential_faulted_and_corrected_versions"
            and (len(phases) < 2 or "healthy" in phases)
        ):
            raise ContractError(f"{scenario['id']}: sequential lifecycle phases are invalid")

    active = [scenario for scenario in catalog["scenarios"] if scenario["status"] == "active"]
    family_counts = Counter(scenario["family"] for scenario in active)
    if len(active) < MINIMUM_ACTIVE_SCENARIOS:
        raise ContractError(
            f"Scenario catalog requires at least {MINIMUM_ACTIVE_SCENARIOS} active scenarios"
        )
    if set(family_counts) != REQUIRED_SCENARIO_FAMILIES:
        missing = sorted(REQUIRED_SCENARIO_FAMILIES - set(family_counts))
        extra = sorted(set(family_counts) - REQUIRED_SCENARIO_FAMILIES)
        raise ContractError(f"Scenario family coverage mismatch; missing={missing}, extra={extra}")
    if family_counts["healthy_controls"] < MINIMUM_HEALTHY_CONTROLS:
        raise ContractError("Scenario catalog requires at least four healthy controls")
    categories = {scenario["expected"]["category"] for scenario in active}
    severities = {scenario["expected"]["severity"] for scenario in active}
    if categories != REQUIRED_CATEGORIES:
        raise ContractError("Scenario catalog does not cover the full category taxonomy")
    if severities != REQUIRED_SEVERITIES:
        raise ContractError("Scenario catalog does not cover every severity")


def _require_exact_keys(data: Any, required: set[str], label: str) -> None:
    if not isinstance(data, dict):
        raise ContractError(f"{label}: expected an object")
    actual = set(data)
    if actual != required:
        raise ContractError(f"{label}: expected keys {sorted(required)}, found {sorted(actual)}")


def validate_supporting_manifests(catalog: dict[str, Any]) -> None:
    scenarios = {scenario["id"]: scenario for scenario in catalog["scenarios"]}
    mutation_recipes: dict[str, tuple[dict[str, Any], str]] = {}
    traffic_recipes: dict[str, tuple[dict[str, Any], str]] = {}
    for path in sorted((ROOT / "scenarios" / "mutations").glob("*.yaml")):
        data = load_data(path)
        label = str(path.relative_to(ROOT))
        _require_exact_keys(data, {"schema_version", "catalog_version", "recipes"}, label)
        if data["schema_version"] != "1.0.0" or data["catalog_version"] != catalog["catalog_version"]:
            raise ContractError(f"{label}: version does not match the scenario catalog")
        for recipe in data["recipes"]:
            _require_exact_keys(
                recipe,
                {
                    "id",
                    "scenario_id",
                    "kind",
                    "description",
                    "agent_types",
                    "operations",
                    "rollback",
                    "synthetic_only",
                },
                label,
            )
            if recipe["id"] in mutation_recipes:
                raise ContractError(f"{label}: duplicate mutation recipe ID {recipe['id']}")
            if recipe["scenario_id"] not in scenarios:
                raise ContractError(f"{label}: unknown scenario_id")
            if recipe["synthetic_only"] is not True or not recipe["operations"]:
                raise ContractError(f"{label}: mutation recipes must be synthetic and actionable")
            mutation_recipes[recipe["id"]] = (recipe, path.relative_to(ROOT).as_posix())
    for path in sorted((ROOT / "scenarios" / "traffic").glob("*.yaml")):
        data = load_data(path)
        label = str(path.relative_to(ROOT))
        _require_exact_keys(
            data,
            {
                "schema_version",
                "catalog_version",
                "endpoint_only",
                "direct_trace_injection",
                "recipes",
            },
            label,
        )
        if data["schema_version"] != "1.0.0" or data["catalog_version"] != catalog["catalog_version"]:
            raise ContractError(f"{label}: version does not match the scenario catalog")
        if data["endpoint_only"] is not True or data["direct_trace_injection"] != "forbidden":
            raise ContractError(f"{label}: traffic must invoke endpoints without telemetry injection")
        for recipe in data["recipes"]:
            _require_exact_keys(
                recipe,
                {
                    "id",
                    "scenario_id",
                    "description",
                    "request_count",
                    "method",
                    "path",
                    "body_template",
                    "expected_endpoint_behavior",
                    "synthetic_data",
                },
                label,
            )
            if recipe["id"] in traffic_recipes:
                raise ContractError(f"{label}: duplicate traffic recipe ID {recipe['id']}")
            if recipe["scenario_id"] not in scenarios:
                raise ContractError(f"{label}: unknown scenario_id")
            if recipe["method"] != "POST" or recipe["path"] != "$AIQ_DEPLOYED_AGENT_ENDPOINT":
                raise ContractError(f"{label}: traffic recipes must POST to the deployed endpoint")
            if recipe["synthetic_data"] is not True:
                raise ContractError(f"{label}: traffic recipes must use synthetic data")
            traffic_recipes[recipe["id"]] = (recipe, path.relative_to(ROOT).as_posix())

    expected_mutations = set()
    expected_traffic = set()
    for scenario in scenarios.values():
        mutation_id = scenario["mutation"]["recipe_id"]
        if mutation_id is not None:
            expected_mutations.add(mutation_id)
            recipe_entry = mutation_recipes.get(mutation_id)
            if recipe_entry is None:
                raise ContractError(f"{scenario['id']}: mutation recipe does not exist")
            recipe, recipe_path = recipe_entry
            if (
                recipe["scenario_id"] != scenario["id"]
                or recipe["kind"] != scenario["mutation"]["kind"]
                or recipe_path != scenario["mutation"]["manifest"]
                or not set(scenario["compatibility"]["agent_types"]).issubset(
                    set(recipe["agent_types"])
                )
            ):
                raise ContractError(f"{scenario['id']}: mutation recipe contract does not match")
        traffic_id = scenario["traffic"]["recipe_id"]
        expected_traffic.add(traffic_id)
        recipe_entry = traffic_recipes.get(traffic_id)
        if recipe_entry is None:
            raise ContractError(f"{scenario['id']}: traffic recipe does not exist")
        recipe, recipe_path = recipe_entry
        if (
            recipe["scenario_id"] != scenario["id"]
            or recipe_path != scenario["traffic"]["recipe"]
            or recipe["request_count"] != scenario["traffic"]["minimum_requests"]
        ):
            raise ContractError(f"{scenario['id']}: traffic recipe contract does not match")
    if set(mutation_recipes) != expected_mutations:
        raise ContractError("Mutation recipe registry contains unreferenced or missing recipes")
    if set(traffic_recipes) != expected_traffic:
        raise ContractError("Traffic recipe registry contains unreferenced or missing recipes")


def validate_reporting_config(data: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "mode",
        "recipient_variable",
        "recipient_variables",
        "allowed_domain",
        "promotion_requires_human_review",
        "automation_can_promote",
    }
    _require_exact_keys(data, required, "config/reporting.yaml")
    expected = {
        "test": "AIQ_TEST_REPORT_RECIPIENT",
        "production": "AIQ_PRODUCTION_REPORT_RECIPIENT",
    }
    if data["schema_version"] != "1.0.0":
        raise ContractError("config/reporting.yaml: unsupported schema_version")
    if data["recipient_variables"] != expected:
        raise ContractError("config/reporting.yaml: recipient variable contract is immutable")
    if data["mode"] not in expected:
        raise ContractError("config/reporting.yaml: mode must be test or production")
    if data["recipient_variable"] != expected[data["mode"]]:
        raise ContractError("config/reporting.yaml: recipient variable must match the selected mode")
    if data["allowed_domain"] != "microsoft.com":
        raise ContractError("config/reporting.yaml: allowed domain must be microsoft.com")
    if data["promotion_requires_human_review"] is not True:
        raise ContractError("config/reporting.yaml: production promotion must require human review")
    if data["automation_can_promote"] is not False:
        raise ContractError("config/reporting.yaml: automation must not be able to promote")


def validate_ado_policy(data: dict[str, Any]) -> None:
    validate_instance(data, SCHEMAS / "ado-policy.schema.json", "config/ado-policy.yaml")
    if data["schema_version"] != "1.0.0" or data["policy_version"] != "1.0.0":
        raise ContractError("config/ado-policy.yaml: unsupported schema or policy version")


def validate_automation_policy(data: dict[str, Any]) -> None:
    _require_exact_keys(
        data,
        {
            "schema_version",
            "generated_branch_prefix",
            "allowed_paths",
            "protected_paths",
        },
        "config/automation-policy.yaml",
    )
    if data["schema_version"] != "1.0.0":
        raise ContractError("config/automation-policy.yaml: unsupported schema_version")
    if data["generated_branch_prefix"] != "aiq-daily/":
        raise ContractError("config/automation-policy.yaml: generated branch prefix must be aiq-daily/")
    mandatory_protected = {
        "config/**",
        "agents/**",
        "scenarios/**",
        "schemas/**",
        "src/**",
        ".github/**",
    }
    if not mandatory_protected.issubset(set(data["protected_paths"])):
        raise ContractError("config/automation-policy.yaml: mandatory protected paths are missing")
    if any(path.startswith("config/") for path in data["allowed_paths"]):
        raise ContractError("config/automation-policy.yaml: generated automation cannot modify config")


def validate_security_policy(data: dict[str, Any]) -> None:
    _require_exact_keys(
        data,
        {
            "schema_version",
            "scan_roots",
            "source_extensions",
            "forbidden_patterns",
        },
        "config/security-policy.yaml",
    )
    if data["schema_version"] != "1.0.0" or not data["forbidden_patterns"]:
        raise ContractError("config/security-policy.yaml: invalid security policy")
    mandatory_roots = {"agents", "infra", "scenarios", "src"}
    mandatory_extensions = {".py", ".ps1", ".sh", ".bicep", ".yaml", ".yml", ".json"}
    if not mandatory_roots.issubset(set(data["scan_roots"])):
        raise ContractError("config/security-policy.yaml: mandatory scan roots are missing")
    if not mandatory_extensions.issubset(set(data["source_extensions"])):
        raise ContractError("config/security-policy.yaml: mandatory source extensions are missing")


def validate_traffic_policy(data: dict[str, Any]) -> None:
    _require_exact_keys(
        data,
        {
            "schema_version",
            "deployed_endpoint_variable",
            "endpoint_invocation_required",
            "application_insights_access",
            "direct_trace_injection",
            "invocation_response_ids_are_trace_ids",
            "trace_correlation_source",
            "forbidden_ingestion_hosts",
        },
        "config/traffic-policy.yaml",
    )
    if data["schema_version"] != "1.0.0":
        raise ContractError("config/traffic-policy.yaml: unsupported schema_version")
    if data["deployed_endpoint_variable"] != "AIQ_DEPLOYED_AGENT_ENDPOINT":
        raise ContractError("config/traffic-policy.yaml: endpoint must be resolved from the protected variable")
    if data["endpoint_invocation_required"] is not True:
        raise ContractError("config/traffic-policy.yaml: deployed endpoint invocation is mandatory")
    if data["application_insights_access"] != "read_only":
        raise ContractError("config/traffic-policy.yaml: Application Insights must be read-only")
    if data["direct_trace_injection"] != "forbidden":
        raise ContractError("config/traffic-policy.yaml: direct trace injection must be forbidden")
    if data["invocation_response_ids_are_trace_ids"] is not False:
        raise ContractError("config/traffic-policy.yaml: invocation and response IDs are not trace IDs")
    if data["trace_correlation_source"] != "application_insights_operation_id":
        raise ContractError("config/traffic-policy.yaml: trace links require operation_Id correlation")
    expected_hosts = {
        "monitor.azure.com",
        "applicationinsights.azure.com",
        "dc.applicationinsights.azure.com",
    }
    if set(data["forbidden_ingestion_hosts"]) != expected_hosts:
        raise ContractError("config/traffic-policy.yaml: forbidden ingestion hosts are incomplete")


def validate_link_policy(data: dict[str, Any]) -> None:
    _require_exact_keys(
        data,
        {
            "schema_version",
            "base_url",
            "agent_route_template",
            "standalone_insights_suffix",
            "fallback_insights_suffix",
            "trace_suffix_template",
            "runtime_fields",
            "invocation_response_ids_are_trace_ids",
            "telemetry_correlation_field",
            "persist_private_links",
        },
        "config/link-policy.yaml",
    )
    expected = {
        "schema_version": "1.0.0",
        "base_url": "https://ai.azure.com",
        "agent_route_template": (
            "/nextgen/r/{subscription},{resource_group},,{account},{project}"
            "/build/agents/{agent}"
        ),
        "standalone_insights_suffix": "/insights",
        "fallback_insights_suffix": "/monitor/insights",
        "trace_suffix_template": "/traces/{operation_id}",
        "runtime_fields": ["subscription", "resource_group", "account", "project"],
        "invocation_response_ids_are_trace_ids": False,
        "telemetry_correlation_field": "operation_Id",
        "persist_private_links": False,
    }
    if data != expected:
        raise ContractError("config/link-policy.yaml: public-safe Agent Insights link contract changed")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def catalog_bundle_hash(
    catalog_path: Path | None = None,
    *,
    catalog: dict[str, Any] | None = None,
    agents: list[dict[str, Any]] | None = None,
) -> str:
    if catalog_path is not None and catalog is not None:
        raise ContractError("Catalog hash accepts a path or an object, not both")
    catalog_data = catalog or load_data(catalog_path or ROOT / "scenarios" / "catalog.yaml")
    inputs: dict[str, bytes] = {"scenarios/catalog.yaml": _canonical_bytes(catalog_data)}

    if agents is None:
        for path in reviewed_source_files(ROOT / "agents"):
            logical_path = path.relative_to(ROOT).as_posix()
            inputs[logical_path] = (
                _canonical_bytes(load_data(path))
                if path.suffix in {".json", ".yaml", ".yml"}
                else path.read_bytes().replace(b"\r\n", b"\n")
            )
    else:
        for agent in agents:
            source_path = agent["implementation"]["source_path"]
            inputs[f"{source_path}/manifest.yaml"] = _canonical_bytes(agent)
            for path in reviewed_source_files(ROOT / source_path):
                if path.name == "manifest.yaml":
                    continue
                logical_path = path.relative_to(ROOT).as_posix()
                inputs[logical_path] = (
                    _canonical_bytes(load_data(path))
                    if path.suffix in {".json", ".yaml", ".yml"}
                    else path.read_bytes().replace(b"\r\n", b"\n")
                )

    for root in (ROOT / "scenarios" / "mutations", ROOT / "scenarios" / "traffic"):
        for path in sorted(root.glob("*.yaml")):
            inputs[path.relative_to(ROOT).as_posix()] = _canonical_bytes(load_data(path))

    digest = hashlib.sha256()
    for logical_path, content in sorted(inputs.items()):
        digest.update(logical_path.encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _catalog_hash() -> str:
    return catalog_bundle_hash()


def selection_policy_hash(policy: dict[str, Any] | None = None) -> str:
    data = policy or load_data(ROOT / "config" / "selection-policy.yaml")
    return "sha256:" + hashlib.sha256(_canonical_bytes(data)).hexdigest()


def mandatory_scenarios_for_weekday(
    active: list[dict[str, Any]],
    policy: dict[str, Any],
    weekday: str,
) -> list[dict[str, Any]]:
    cadence = policy["selection"]["scenario_cadence"]
    priorities = set(policy["selection"]["mandatory_fault_priorities"])
    return [
        scenario
        for scenario in active
        if scenario["expected"]["category"] == "none"
        or (
            scenario["priority"] in priorities
            and (
                scenario["id"] not in cadence
                or weekday in cadence[scenario["id"]]
            )
        )
    ]


def validate_selection_policy(
    data: dict[str, Any],
    catalog: dict[str, Any] | None = None,
) -> None:
    validate_instance(data, SELECTION_POLICY_SCHEMA, "config/selection-policy.yaml")
    cycle = data["cycle"]
    epoch = date.fromisoformat(cycle["epoch_monday"])
    if epoch.weekday() != 0:
        raise ContractError(
            "config/selection-policy.yaml: cycle epoch must be a Monday"
        )
    if sum(cycle["partition_scenario_counts"]) != 47:
        raise ContractError(
            "config/selection-policy.yaml: rotating partitions must cover 47 scenarios"
        )
    if set(data["selection"]["mandatory_fault_priorities"]) & set(
        data["selection"]["rotating_fault_priorities"]
    ):
        raise ContractError(
            "config/selection-policy.yaml: mandatory and rotating priorities must be disjoint"
        )
    if set(data["selection"]["required_fault_categories"]) != REQUIRED_CATEGORIES - {"none"}:
        raise ContractError(
            "config/selection-policy.yaml: required categories must match the fault taxonomy"
        )
    if data["scheduled_automation"]["weekdays"] != cycle["weekdays"]:
        raise ContractError(
            "config/selection-policy.yaml: scheduled automation must use the reviewed weekdays"
        )
    if catalog is None:
        return
    active = [scenario for scenario in catalog["scenarios"] if scenario["status"] == "active"]
    controls = [
        scenario for scenario in active if scenario["expected"]["category"] == "none"
    ]
    mandatory = [
        scenario
        for scenario in active
        if scenario["expected"]["category"] != "none"
        and scenario["priority"] in data["selection"]["mandatory_fault_priorities"]
    ]
    rotating = [
        scenario
        for scenario in active
        if scenario["expected"]["category"] != "none"
        and scenario["priority"] in data["selection"]["rotating_fault_priorities"]
    ]
    classified = {scenario["id"] for scenario in controls + mandatory + rotating}
    if classified != {scenario["id"] for scenario in active}:
        raise ContractError(
            "config/selection-policy.yaml: every active scenario must be classified"
        )
    if len(controls) != 6 or len(mandatory) != 10 or len(rotating) != 47:
        raise ContractError(
            "config/selection-policy.yaml: expected 6 controls, 10 mandatory faults, and 47 rotating faults"
        )
    cadence = data["selection"]["scenario_cadence"]
    if set(cadence) != {"aiq-scn-062-umbrella-insight"}:
        raise ContractError(
            "config/selection-policy.yaml: only the reviewed umbrella probe has a special cadence"
        )
    umbrella = next(
        scenario
        for scenario in mandatory
        if scenario["id"] == "aiq-scn-062-umbrella-insight"
    )
    if expected_finding_count(umbrella) != 2:
        raise ContractError(
            "config/selection-policy.yaml: umbrella probe must retain two expected roots"
        )
    ordinary_p0 = [scenario for scenario in mandatory if scenario is not umbrella]
    if len(ordinary_p0) != 9 or any(
        expected_finding_count(scenario) != 1 for scenario in ordinary_p0
    ):
        raise ContractError(
            "config/selection-policy.yaml: nine single-root P0 faults must run every weekday"
        )
    daily_capacity = len(EXPECTED_AGENTS) * data["limits"][
        "expected_insight_cap_per_agent"
    ]
    expected_totals = [20, 19, 20, 19, 20]
    actual_totals = []
    for weekday, rotating_count in zip(
        cycle["weekdays"],
        cycle["partition_scenario_counts"],
        strict=True,
    ):
        selected_mandatory = mandatory_scenarios_for_weekday(active, data, weekday)
        mandatory_roots = sum(
            expected_finding_count(scenario)
            for scenario in selected_mandatory
            if scenario["expected"]["category"] != "none"
        )
        actual_totals.append(mandatory_roots + rotating_count)
    if actual_totals != expected_totals or max(actual_totals) > daily_capacity:
        raise ContractError(
            "config/selection-policy.yaml: weekday selections must total 20/19/20/19/20 expected roots"
        )


def load_selection_policy(catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = load_data(ROOT / "config" / "selection-policy.yaml")
    validate_selection_policy(policy, catalog)
    return policy


def validate_daily_plan_semantics(
    plan: dict[str, Any],
    agents: list[dict[str, Any]],
    catalog: dict[str, Any],
    label: str,
    expected_catalog_hash: str | None = None,
    expected_policy: dict[str, Any] | None = None,
    allow_historical: bool = False,
) -> None:
    policy = expected_policy or load_selection_policy(catalog)
    policy_digest = selection_policy_hash(policy)
    agent_by_id = {agent["id"]: agent for agent in agents}
    scenario_by_id = {scenario["id"]: scenario for scenario in catalog["scenarios"]}
    active_ids = {
        scenario["id"] for scenario in catalog["scenarios"] if scenario["status"] == "active"
    }
    assignment_ids = [assignment["scenario_id"] for assignment in plan["assignments"]]
    catalog_historical = plan["catalog_version"] != catalog["catalog_version"]
    policy_historical = (
        plan["policy_version"] != policy["policy_version"]
        or plan["policy_hash"] != policy_digest
    )
    historical = catalog_historical or policy_historical
    if historical and not allow_historical:
        raise ContractError(
            f"{label}: catalog_version or selection policy does not match current contracts"
        )
    if not catalog_historical and plan["catalog_hash"] != (
        expected_catalog_hash or _catalog_hash()
    ):
        raise ContractError(f"{label}: catalog_hash does not match scenarios/catalog.yaml")
    expected_seed = int(
        hashlib.sha256(
            (
                f"{plan['report_date']}:{plan['catalog_hash']}:{plan['policy_hash']}"
            ).encode("ascii")
        ).hexdigest()[:16],
        16,
    )
    if plan["seed"] != expected_seed:
        raise ContractError(f"{label}: seed does not match report date and catalog hash")
    plan_id_pattern = rf"aiq-{plan['report_date'].replace('-', '')}(?:-r[0-9]{{2}})?"
    if re.fullmatch(plan_id_pattern, plan["plan_id"]) is None:
        raise ContractError(f"{label}: plan_id does not match report date")
    base_plan_id = f"aiq-{plan['report_date'].replace('-', '')}"
    expected_artifact_directory = (
        f"reports/daily/{plan['report_date'].replace('-', '/')}"
        + (f"/{plan['plan_id']}" if plan["plan_id"] != base_plan_id else "")
    )
    if plan["artifact_directory"] != expected_artifact_directory:
        raise ContractError(f"{label}: artifact directory does not preserve plan identity")
    if plan["project"]["name"] != plan["plan_id"]:
        raise ContractError(f"{label}: project name does not match plan identity")
    expected_project_reference = "sha256:" + hashlib.sha256(
        f"runtime:project:{plan['plan_id']}".encode("ascii")
    ).hexdigest()
    if plan["project"]["resource_reference"] != expected_project_reference:
        raise ContractError(f"{label}: project reference does not match plan identity")
    expected_expiry = (
        date.fromisoformat(plan["report_date"]) + timedelta(days=7)
    ).isoformat()
    if plan["project"]["expires_on"] != expected_expiry:
        raise ContractError(f"{label}: project retention must be seven days")
    digest_input = {key: value for key, value in plan.items() if key != "plan_digest"}
    expected_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            digest_input,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    if plan["plan_digest"] != expected_digest:
        raise ContractError(f"{label}: plan_digest is not canonical")
    if len(assignment_ids) != len(set(assignment_ids)):
        raise ContractError(f"{label}: each scenario may be assigned only once")
    selection = plan["selection"]
    selected_ids = selection["selected_scenario_ids"]
    omitted_ids = selection["omitted_scenario_ids"]
    if assignment_ids != selected_ids:
        raise ContractError(f"{label}: assignment order must match selected scenario order")
    if len(selected_ids) != len(set(selected_ids)) or len(omitted_ids) != len(set(omitted_ids)):
        raise ContractError(f"{label}: selected and omitted scenario IDs must be unique")
    if set(selected_ids) & set(omitted_ids):
        raise ContractError(f"{label}: selected and omitted scenarios overlap")
    if not historical and set(selected_ids) | set(omitted_ids) != active_ids:
        raise ContractError(f"{label}: selected and omitted scenarios must partition the active catalog")
    if set(selection["selection_reasons"]) != set(selected_ids):
        raise ContractError(f"{label}: every selected scenario needs one selection reason")
    report_day = date.fromisoformat(plan["report_date"])
    epoch = date.fromisoformat(policy["cycle"]["epoch_monday"])
    expected_cycle_number = (report_day - epoch).days // 7
    weekday_index = report_day.weekday()
    expected_cycle_index = weekday_index if weekday_index < 5 else None
    expected_weekday = (
        policy["cycle"]["weekdays"][weekday_index]
        if weekday_index < 5
        else "non_scheduled"
    )
    expected_cycle_id = (
        f"cycle-{expected_cycle_number}-"
        + hashlib.sha256(
            f"{plan['catalog_hash']}:{plan['policy_hash']}:{expected_cycle_number}".encode(
                "ascii"
            )
        ).hexdigest()[:12]
    )
    cycle = selection["cycle"]
    if not policy_historical and (
        cycle["id"] != expected_cycle_id
        or cycle["number"] != expected_cycle_number
        or cycle["business_day"] != (
            expected_cycle_index + 1 if expected_cycle_index is not None else None
        )
        or cycle["weekday"] != expected_weekday
        or cycle["length_business_days"] != policy["cycle"]["business_days"]
        or cycle["full_coverage_horizon_business_days"]
        != policy["cycle"]["business_days"]
    ):
        raise ContractError(f"{label}: selection cycle metadata is not deterministic")
    active = [
        scenario for scenario in catalog["scenarios"] if scenario["status"] == "active"
    ]
    mandatory_ids = {
        scenario["id"]
        for scenario in mandatory_scenarios_for_weekday(
            active,
            policy,
            expected_weekday,
        )
    }
    rotating_ids = {
        scenario["id"]
        for scenario in catalog["scenarios"]
        if scenario["status"] == "active"
        and scenario["expected"]["category"] != "none"
        and scenario["priority"] in policy["selection"]["rotating_fault_priorities"]
    }
    if not policy_historical and plan["selection_mode"] == "rotating_daily":
        if expected_cycle_index is None:
            raise ContractError(
                f"{label}: rotating daily plans are allowed only Monday through Friday"
            )
        expected_rotating_count = policy["cycle"]["partition_scenario_counts"][
            expected_cycle_index
        ]
        if not plan["human_daily_contract"] or not plan["limits"]["expected_cap_enforced"]:
            raise ContractError(f"{label}: rotating daily plans must claim the expected-cap contract")
        if set(selection["mandatory_scenario_ids"]) != mandatory_ids:
            raise ContractError(f"{label}: rotating plan is missing a mandatory daily scenario")
        if len(selection["rotating_scenario_ids"]) != expected_rotating_count:
            raise ContractError(f"{label}: rotating partition has the wrong scenario count")
        if not set(selection["rotating_scenario_ids"]).issubset(rotating_ids):
            raise ContractError(f"{label}: rotating partition contains a non-rotating scenario")
        if set(selected_ids) != mandatory_ids | set(selection["rotating_scenario_ids"]):
            raise ContractError(f"{label}: selected scenarios do not match mandatory plus rotating")
    elif not policy_historical and plan["selection_mode"] == "full_catalog":
        if plan["human_daily_contract"] or plan["limits"]["expected_cap_enforced"]:
            raise ContractError(f"{label}: full catalog cannot claim the human daily cap")
        if set(selected_ids) != active_ids or omitted_ids:
            raise ContractError(f"{label}: full catalog mode must select every active scenario")
    elif not policy_historical:
        raise ContractError(f"{label}: unknown selection mode")
    if historical and len(assignment_ids) != plan["coverage"]["scenario_count"]:
        raise ContractError(f"{label}: historical assignment count contradicts coverage")

    run_assignments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    load_counts = Counter(assignment["agent_id"] for assignment in plan["assignments"])
    for assignment in plan["assignments"]:
        scenario = scenario_by_id.get(assignment["scenario_id"])
        agent = agent_by_id.get(assignment["agent_id"])
        if not historical:
            if scenario is None:
                raise ContractError(f"{label}: assignment references an unknown scenario")
            if agent is None:
                raise ContractError(f"{label}: assignment references an unknown agent")
            if assignment["scenario_version"] != scenario["version"]:
                raise ContractError(f"{label}: scenario version does not match the catalog")
            if assignment["family"] != scenario["family"]:
                raise ContractError(f"{label}: scenario family does not match the catalog")
            if assignment["selection_reason"] != selection["selection_reasons"][
                assignment["scenario_id"]
            ]:
                raise ContractError(f"{label}: assignment selection reason does not match")
            if assignment["conflict_tags"] != scenario["conflict_tags"]:
                raise ContractError(f"{label}: conflict tags do not match the catalog")
            if not assignment["agent_name"].startswith(agent["required_name_prefix"]):
                raise ContractError(f"{label}: agent name does not use its required prefix")
            compatibility = scenario["compatibility"]
            if agent["domain"] not in compatibility["domains"]:
                raise ContractError(f"{label}: scenario is incompatible with the assigned agent domain")
            if agent["agent_type"] not in compatibility["agent_types"]:
                raise ContractError(f"{label}: scenario is incompatible with the assigned agent type")
            if compatibility["agent_ids"] and agent["id"] not in compatibility["agent_ids"]:
                raise ContractError(f"{label}: scenario is incompatible with the assigned agent ID")
            if assignment["agent_type"] != agent["agent_type"]:
                raise ContractError(f"{label}: assignment agent type does not match its manifest")
            if assignment["traffic_recipe_id"] != scenario["traffic"]["recipe_id"]:
                raise ContractError(f"{label}: traffic recipe does not match the scenario")
            if not policy_historical and (
                assignment["traffic_seed_namespace"]
                != scenario["traffic"]["seed_namespace"]
            ):
                raise ContractError(f"{label}: traffic seed namespace does not match the scenario")
            if assignment["traffic_requests"] != scenario["traffic"]["minimum_requests"]:
                raise ContractError(f"{label}: traffic request count does not match the scenario")
            if assignment["lifecycle"] != scenario["version_semantics"]["applies_to"]:
                raise ContractError(f"{label}: lifecycle does not match the scenario")
            expected = assignment["expected"]
            scenario_expected = scenario["expected"]
            if (
                expected["category"] != scenario_expected["category"]
                or expected["severity"] != scenario_expected["severity"]
                or expected["validation_targets"] != scenario_expected["validation_targets"]
                or expected["finding_count"] != expected_finding_count(scenario)
            ):
                raise ContractError(f"{label}: expected finding contract does not match the scenario")
        expected_traffic_seed = int(
            hashlib.sha256(
                (
                    f"{plan['seed']}:{assignment['traffic_seed_namespace']}"
                ).encode("ascii")
            ).hexdigest()[:16],
            16,
        )
        if assignment["traffic_seed"] != expected_traffic_seed:
            raise ContractError(f"{label}: assignment traffic seed is not deterministic")
        sequence = assignment["version_sequence"]
        sequential = (
            assignment["lifecycle"] == "sequential_faulted_and_corrected_versions"
        )
        if sequential and len(sequence) < 2:
            raise ContractError(f"{label}: sequential lifecycle requires multiple versions")
        if not sequential and len(sequence) != 1:
            raise ContractError(f"{label}: non-sequential lifecycle requires one version")
        if assignment["agent_version_digest"] != sequence[0]["digest"]:
            raise ContractError(f"{label}: primary version digest must start the version sequence")
        if assignment["window"] != sequence[0]["window"]:
            raise ContractError(f"{label}: primary window must start the version sequence")
        if not historical and [version["phase"] for version in sequence] != scenario[
            "version_semantics"
        ]["phases"]:
            raise ContractError(f"{label}: version phases do not match the scenario")
        if not historical and [version["version_key"] for version in sequence] != scenario[
            "version_semantics"
        ]["version_keys"]:
            raise ContractError(f"{label}: version identities do not match the scenario")
        key_to_digest: dict[str, str] = {}
        digest_to_key: dict[str, str] = {}
        for version in sequence:
            prior_digest = key_to_digest.setdefault(version["version_key"], version["digest"])
            if prior_digest != version["digest"]:
                raise ContractError(
                    f"{label}: repeated version key must reuse its immutable digest"
                )
            prior_key = digest_to_key.setdefault(version["digest"], version["version_key"])
            if prior_key != version["version_key"]:
                raise ContractError(
                    f"{label}: distinct version keys must use distinct immutable digests"
                )
            expected_window = {
                "start": (
                    f"window://{assignment['run_id']}/{version['phase']}/start-inclusive"
                ),
                "end": (
                    f"window://{assignment['run_id']}/{version['phase']}/end-exclusive"
                ),
            }
            if version["window"] != expected_window:
                raise ContractError(f"{label}: version window placeholder is not exact")
        run_assignments[assignment["run_id"]].append(assignment)

    if not historical and set(load_counts) != set(agent_by_id):
        raise ContractError(f"{label}: every registered agent must receive an assignment")
    if max(load_counts.values()) - min(load_counts.values()) > 1:
        raise ContractError(f"{label}: assignments are not balanced across agents")
    for run_id, assignments in run_assignments.items():
        if len({item["agent_id"] for item in assignments}) != 1:
            raise ContractError(f"{label}: run {run_id} spans multiple agents")
        if sum(item["expected"]["finding_count"] for item in assignments) > 4:
            raise ContractError(f"{label}: run {run_id} exceeds four injected root causes")
        if len({item["wave"] for item in assignments}) != 1:
            raise ContractError(f"{label}: run {run_id} spans multiple waves")
        if len({item["agent_version_digest"] for item in assignments}) != 1:
            raise ContractError(f"{label}: run {run_id} spans multiple immutable versions")
        if len(
            {
                (item["window"]["start"], item["window"]["end"])
                for item in assignments
            }
        ) != 1:
            raise ContractError(f"{label}: run {run_id} spans multiple analysis windows")
        if len(
            {
                item["version_sequence"][0]["phase"]
                for item in assignments
            }
        ) != 1:
            raise ContractError(f"{label}: run {run_id} mixes healthy and faulted phases")
        seen_conflicts: set[str] = set()
        for assignment in assignments:
            conflicts = set(assignment["conflict_tags"])
            if not seen_conflicts.isdisjoint(conflicts):
                raise ContractError(f"{label}: run {run_id} co-locates conflicting scenarios")
            seen_conflicts.update(conflicts)
        if any(
            item["lifecycle"] == "sequential_faulted_and_corrected_versions"
            for item in assignments
        ) and len(assignments) != 1:
            raise ContractError(f"{label}: sequential lifecycle runs must be isolated")

    per_agent_expected = {
        agent_id: sum(
            assignment["expected"]["finding_count"]
            for assignment in plan["assignments"]
            if assignment["agent_id"] == agent_id
        )
        for agent_id in sorted(agent_by_id)
    }
    if plan["per_agent_expected_totals"] != per_agent_expected:
        raise ContractError(f"{label}: per-agent expected totals do not match assignments")
    limits = plan["limits"]
    if (
        limits["expected_insight_cap_per_agent"]
        != policy["limits"]["expected_insight_cap_per_agent"]
        or limits["expected_root_cap_per_run"]
        != policy["limits"]["expected_root_cap_per_run"]
        or limits["actual_insight_count_rule"]
        != policy["limits"]["actual_insight_count_rule"]
    ):
        raise ContractError(f"{label}: plan limits do not match selection policy")
    fault_assignments = [
        assignment
        for assignment in plan["assignments"]
        if assignment["expected"]["finding_count"] > 0
    ]
    if not policy_historical and plan["selection_mode"] == "rotating_daily":
        if len(fault_assignments) > policy["limits"]["daily_fault_scenario_count"]:
            raise ContractError(f"{label}: daily fault scenario budget exceeded")
        if sum(item["expected"]["finding_count"] for item in fault_assignments) > policy[
            "limits"
        ]["daily_expected_root_count"]:
            raise ContractError(f"{label}: daily expected root budget exceeded")
        if max(per_agent_expected.values()) > limits["expected_insight_cap_per_agent"]:
            raise ContractError(f"{label}: agent exceeds four expected root causes")

    selected_scenarios = [
        scenario_by_id[scenario_id]
        for scenario_id in selected_ids
        if scenario_id in scenario_by_id
    ]
    expected_coverage = {
        "scenario_count": len(plan["assignments"]),
        "healthy_control_count": sum(
            assignment["expected"]["finding_count"] == 0
            for assignment in plan["assignments"]
        ),
        "families": sorted({scenario["family"] for scenario in selected_scenarios}),
        "categories": sorted(
            {scenario["expected"]["category"] for scenario in selected_scenarios}
        ),
        "severities": sorted(
            {scenario["expected"]["severity"] for scenario in selected_scenarios}
        ),
        "agent_types": sorted(
            {assignment["agent_type"] for assignment in plan["assignments"]}
        ),
    }
    if plan["coverage"] != expected_coverage:
        raise ContractError(f"{label}: declared coverage does not match assignments")


def validate_bug_action_semantics(report: dict[str, Any], label: str) -> None:
    mutation_actions = {"created", "updated", "reopened", "commented"}
    for action in report.get("bug_actions", []):
        mutation = action["action"] in mutation_actions
        policy_enabled = action["policy_snapshot"]["auto_apply_enabled"]
        receipt = action["apply_receipt"]
        if not policy_enabled and action["action"] not in {"candidate", "none"}:
            raise ContractError(
                f"{label}: disabled ADO policy can report only candidate or none"
            )
        if mutation:
            if report["status"] == "INCONCLUSIVE":
                raise ContractError(
                    f"{label}: INCONCLUSIVE report cannot claim an ADO mutation"
                )
            if (
                not policy_enabled
                or receipt is None
                or receipt["confirmed"] is not True
                or action["work_item_reference"] is None
                or receipt["work_item_reference"] != action["work_item_reference"]
            ):
                raise ContractError(
                    f"{label}: ADO mutation requires enabled policy and a matching confirmed receipt"
                )
        elif receipt is not None:
            raise ContractError(
                f"{label}: candidate or none ADO action cannot contain an apply receipt"
            )


def validate_canonical_report_semantics(
    report: dict[str, Any],
    agents: list[dict[str, Any]],
    catalog: dict[str, Any],
    label: str,
    *,
    expected_scenario_ids: set[str] | None = None,
) -> None:
    from agent_insights_quality.reporting.model import (
        STRUCTURED_REPORT_VERSION,
        validate_structured_bar,
    )

    validate_structured_bar(report, label)
    agent_by_id = {agent["id"]: agent for agent in agents}
    registered_ids = set(agent_by_id)
    reported_ids = [agent["id"] for agent in report["agents"]]
    if len(reported_ids) != len(set(reported_ids)) or set(reported_ids) != registered_ids:
        raise ContractError(f"{label}: report must include every registered agent exactly once")
    for reported_agent in report["agents"]:
        if reported_agent["type"] != agent_by_id[reported_agent["id"]]["agent_type"]:
            raise ContractError(f"{label}: reported agent type does not match its manifest")
        if not reported_agent["name"].startswith(
            agent_by_id[reported_agent["id"]]["required_name_prefix"]
        ):
            raise ContractError(f"{label}: reported agent name does not use its required prefix")

    scenario_by_id = {scenario["id"]: scenario for scenario in catalog["scenarios"]}
    if report.get("schema_version") == STRUCTURED_REPORT_VERSION:
        checklists = report["human_validation_checklists"]
        checklist_ids = [item["agent_id"] for item in checklists]
        if (
            len(checklist_ids) != len(set(checklist_ids))
            or set(checklist_ids) != registered_ids
        ):
            raise ContractError(
                f"{label}: human validation requires exactly one checklist per report agent"
            )
        reported_agents = {item["id"]: item for item in report["agents"]}
        for checklist in checklists:
            agent_id = checklist["agent_id"]
            if checklist["review_reason"] != reported_agents[agent_id]["human_validation"]:
                raise ContractError(
                    f"{label}: checklist review reason contradicts derived human validation"
                )
            for version in checklist["versions"]:
                for expected in version["expected_scenarios"]:
                    scenario = scenario_by_id.get(expected["scenario_id"])
                    if scenario is None:
                        raise ContractError(
                            f"{label}: checklist references an unknown scenario"
                        )
                    if (
                        expected["title"] != scenario["title"]
                        or expected["root_cause"]
                        != scenario["expected"]["root_cause"]
                        or expected["category"] != scenario["expected"]["category"]
                        or expected["severity"] != scenario["expected"]["severity"]
                    ):
                        raise ContractError(
                            f"{label}: checklist ground truth contradicts the scenario contract"
                        )
    active_ids = {
        scenario["id"] for scenario in catalog["scenarios"] if scenario["status"] == "active"
    }
    expected_ids = expected_scenario_ids if expected_scenario_ids is not None else active_ids
    if not expected_ids.issubset(active_ids):
        raise ContractError(f"{label}: expected scenario set contains a non-active scenario")
    result_ids = [result["scenario_id"] for result in report["scenario_results"]]
    if len(result_ids) != len(set(result_ids)):
        raise ContractError(f"{label}: scenario results must be unique")
    if expected_scenario_ids is not None and set(result_ids) != expected_ids:
        missing = sorted(expected_ids - set(result_ids))
        extra = sorted(set(result_ids) - expected_ids)
        raise ContractError(
            f"{label}: scenario results must exactly match selected plan assignments; "
            f"missing={missing}, extra={extra}"
        )
    if not set(result_ids).issubset(expected_ids):
        raise ContractError(f"{label}: report contains an unselected or unknown scenario")
    for result in report["scenario_results"]:
        if result["agent_id"] not in registered_ids:
            raise ContractError(f"{label}: scenario result references an unknown agent")
        scenario = scenario_by_id[result["scenario_id"]]
        agent = agent_by_id[result["agent_id"]]
        compatibility = scenario["compatibility"]
        if (
            agent["domain"] not in compatibility["domains"]
            or agent["agent_type"] not in compatibility["agent_types"]
            or (
                compatibility["agent_ids"]
                and agent["id"] not in compatibility["agent_ids"]
            )
        ):
            raise ContractError(f"{label}: scenario result uses an incompatible agent")
        expected_count = expected_finding_count(scenario)
        if result["expected_count"] != expected_count:
            raise ContractError(
                f"{label}: scenario expected_count does not match the catalog"
            )
        references = result["insight_references"]
        if result["agent_version_digest"] != result["version_sequence"]["version_digest"]:
            raise ContractError(
                f"{label}: scenario result version context is inconsistent"
            )
        if result["observed_count"] != len(references):
            raise ContractError(
                f"{label}: scenario observed_count does not match its insight references"
            )
        if not result["completed"] and result["verdict"] != "inconclusive":
            raise ContractError(f"{label}: incomplete scenario result must be inconclusive")
        if result["verdict"] in {"partially_useful", "incorrect_noise", "mixed"} and not references:
            raise ContractError(f"{label}: produced insight verdict requires an opaque reference")
        if result["verdict"] == "missed" and (
            references or expected_count == 0
        ):
            raise ContractError(f"{label}: missed result must have expected but no observed insights")
    physical_references = [
        (result["run_id"], result["agent_id"], reference)
        for result in report["scenario_results"]
        for reference in result["insight_references"]
    ]
    if len(physical_references) != len(set(physical_references)):
        raise ContractError(
            f"{label}: one physical run insight cannot belong to multiple scenarios"
        )

    scorecard = report["scorecard"]
    counts = scorecard["counts"]
    rates = scorecard["rates"]
    completed_count = sum(result["completed"] for result in report["scenario_results"])
    trust_failures = TRUST_FAILURES & set(scorecard["violations"])
    complete = (
        set(result_ids) == expected_ids
        and completed_count == len(expected_ids)
        and all(result["verdict"] != "inconclusive" for result in report["scenario_results"])
        and report.get("failure") is None
        and not trust_failures
    )
    if trust_failures and (
        report["status"] != "INCONCLUSIVE" or scorecard["complete"]
    ):
        raise ContractError(
            f"{label}: trust failures require INCONCLUSIVE status and incomplete scorecard"
        )
    if scorecard["complete"] != complete:
        raise ContractError(
            f"{label}: scorecard completeness does not match scenario results"
        )
    fault_results = [
        result
        for result in report["scenario_results"]
        if scenario_by_id[result["scenario_id"]]["expected"]["category"] != "none"
    ]
    field_judgments = report.get("field_judgments", [])
    fault_ids = {result["scenario_id"] for result in fault_results}
    low_confidence = any(item["confidence"] < 0.80 for item in field_judgments)
    if low_confidence and (
        report["status"] != "INCONCLUSIVE"
        or "unresolved_judgment" not in scorecard["violations"]
    ):
        raise ContractError(
            f"{label}: low-confidence judgment requires an INCONCLUSIVE report"
        )
    if "field_judgments" in report:
        trusted_counts_by_scenario = {
            scenario_id: min(
                sum(
                    item["scenario_id"] == scenario_id
                    and item["verdict"] == "correct"
                    and item["confidence"] >= 0.80
                    and all(item["attributes"].values())
                    for item in field_judgments
                ),
                expected_finding_count(scenario_by_id[scenario_id]),
            )
            for scenario_id in fault_ids
        }
        partially_useful = sum(
            item["verdict"] == "partially_useful" for item in field_judgments
        )
    else:
        trusted_counts_by_scenario = {
            result["scenario_id"]: (
                expected_finding_count(scenario_by_id[result["scenario_id"]])
                if result["verdict"] == "correct"
                else 0
            )
            for result in fault_results
        }
        partially_useful = sum(
            result["verdict"] == "partially_useful"
            for result in report["scenario_results"]
        )
    true_positives = sum(trusted_counts_by_scenario.values())
    expected_fault_count = sum(
        expected_finding_count(scenario_by_id[result["scenario_id"]])
        for result in fault_results
    )
    false_negatives = expected_fault_count - true_positives
    produced_insights = len(
        {
            (result["run_id"], result["agent_id"], reference)
            for result in report["scenario_results"]
            for reference in result["insight_references"]
        }
    )
    false_positives = max(0, produced_insights - true_positives)
    healthy_insights = len(
        {
            (result["run_id"], result["agent_id"], reference)
            for result in report["scenario_results"]
            if scenario_by_id[result["scenario_id"]]["expected"]["category"] == "none"
            for reference in result["insight_references"]
        }
    )
    if report.get("failure") is not None and not field_judgments:
        true_positives = 0
        partially_useful = 0
        false_positives = 0
        false_negatives = 0
        healthy_insights = 0
    expected_high = [
        result
        for result in fault_results
        if scenario_by_id[result["scenario_id"]]["expected"]["severity"] == "high"
    ]

    def ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 1.0

    expected_rates = {
        "high_severity_recall": ratio(
            sum(
                min(
                    trusted_counts_by_scenario[result["scenario_id"]],
                    expected_finding_count(scenario_by_id[result["scenario_id"]]),
                )
                for result in expected_high
            ),
            sum(
                expected_finding_count(scenario_by_id[result["scenario_id"]])
                for result in expected_high
            ),
        ),
        "overall_recall": ratio(
            true_positives,
            expected_fault_count,
        ),
        "precision": ratio(true_positives, produced_insights),
    }
    for severity in ("medium", "low"):
        rate_name = f"{severity}_severity_recall"
        if rate_name not in rates:
            continue
        expected_severity = [
            result
            for result in fault_results
            if scenario_by_id[result["scenario_id"]]["expected"]["severity"] == severity
        ]
        expected_rates[rate_name] = ratio(
            sum(
                min(
                    trusted_counts_by_scenario[result["scenario_id"]],
                    expected_finding_count(scenario_by_id[result["scenario_id"]]),
                )
                for result in expected_severity
            ),
            sum(
                expected_finding_count(scenario_by_id[result["scenario_id"]])
                for result in expected_severity
            ),
        )
    healthy_results = [
        result
        for result in report["scenario_results"]
        if scenario_by_id[result["scenario_id"]]["expected"]["category"] == "none"
    ]
    if "healthy_noise_rate" in rates:
        expected_rates["healthy_noise_rate"] = ratio(
            sum(bool(result["insight_references"]) for result in healthy_results),
            len(healthy_results),
        )
    precision = expected_rates["precision"]
    recall = expected_rates["overall_recall"]
    expected_rates["f1"] = (
        0.0
        if precision == 0 and recall == 0 and produced_insights > 0 and expected_fault_count > 0
        else ratio(2 * precision * recall, precision + recall)
    )

    if "field_judgments" in report:
        expected_attributes = {
            "root_cause",
            "title",
            "description",
            "proposed_fix",
            "category",
            "severity",
            "linked_traces",
            "meaningfulness",
            "evidence_localization",
            "actionability",
        }
        result_references = {
            (result["scenario_id"], reference)
            for result in report["scenario_results"]
            for reference in result["insight_references"]
        }
        field_keys = [
            (item["scenario_id"], item["insight_reference"])
            for item in report["field_judgments"]
        ]
        physical_field_references = [
            item["insight_reference"] for item in report["field_judgments"]
        ]
        expected_field_keys = result_references
        if (
            len(field_keys) != len(set(field_keys))
            or len(physical_field_references) != len(set(physical_field_references))
            or set(field_keys) != expected_field_keys
        ):
            raise ContractError(f"{label}: field judgments do not match unique insight mappings")
        for item in report["field_judgments"]:
            if set(item["attributes"]) != expected_attributes:
                raise ContractError(f"{label}: field judgment attribute set is incomplete")
        for attribute, rate_name in {
            "category": "category_accuracy",
            "severity": "severity_accuracy",
            "title": "title_pass_rate",
            "description": "description_pass_rate",
            "proposed_fix": "proposed_fix_pass_rate",
            "linked_traces": "linked_trace_pass_rate",
            "evidence_localization": "evidence_localization_rate",
            "meaningfulness": "meaningfulness_rate",
            "actionability": "actionability_rate",
        }.items():
            mapped_expected = [
                item
                for item in report["field_judgments"]
                if item["scenario_id"] in fault_ids
                and item["verdict"] != "incorrect_noise"
            ]
            expected_rates[rate_name] = ratio(
                sum(item["attributes"][attribute] for item in mapped_expected),
                len(mapped_expected),
            )
        judgments_by_scenario: dict[str, list[dict[str, Any]]] = {
            result["scenario_id"]: [] for result in report["scenario_results"]
        }
        for item in field_judgments:
            judgments_by_scenario[item["scenario_id"]].append(item)
        for result in report["scenario_results"]:
            judgments_for_result = judgments_by_scenario[result["scenario_id"]]
            if not result["completed"]:
                expected_verdict = "inconclusive"
            elif result["expected_count"] == 0:
                expected_verdict = (
                    "correct" if result["observed_count"] == 0 else "incorrect_noise"
                )
            elif result["observed_count"] == 0:
                expected_verdict = "missed"
            else:
                trusted = [
                    item
                    for item in judgments_for_result
                    if item["verdict"] == "correct"
                    and item["confidence"] >= 0.80
                    and all(item["attributes"].values())
                ]
                verdicts = {item["verdict"] for item in judgments_for_result}
                if (
                    len(trusted) == result["expected_count"]
                    and result["observed_count"] == result["expected_count"]
                    and verdicts == {"correct"}
                ):
                    expected_verdict = "correct"
                elif not trusted and verdicts == {"incorrect_noise"}:
                    expected_verdict = "incorrect_noise"
                elif not trusted and verdicts == {"partially_useful"}:
                    expected_verdict = "partially_useful"
                else:
                    expected_verdict = "mixed"
            if result["verdict"] != expected_verdict:
                raise ContractError(
                    f"{label}: scenario verdict contradicts its counts or field judgments"
                )

    if "collection_analysis" in report:
        collection = report["collection_analysis"]
        judgments = report.get("field_judgments", [])
        expected_collection = {
            "distinct": sum(not any(item["relationships"].values()) for item in judgments),
            "duplicates": sum(item["relationships"]["duplicate"] for item in judgments),
            "fragments": sum(item["relationships"]["fragment"] for item in judgments),
            "umbrellas": sum(item["relationships"]["umbrella"] for item in judgments),
            "stale_version": int(any(item["stale_version"] for item in judgments)),
        }
        if collection != expected_collection:
            raise ContractError(
                f"{label}: collection analysis does not match field judgments"
            )
        expected_rates.update(
            {
                "distinctness_rate": ratio(collection["distinct"], produced_insights),
                "duplication_rate": ratio(collection["duplicates"], produced_insights),
                "fragmentation_rate": ratio(collection["fragments"], produced_insights),
                "umbrella_rate": ratio(collection["umbrellas"], produced_insights),
                "cross_version_stale_rate": ratio(
                    collection["stale_version"],
                    len(report["scenario_results"]),
                ),
            }
        )

    if report["status"] != scorecard["verdict"]:
        raise ContractError(f"{label}: report status and scorecard verdict must match")
    validate_bug_action_semantics(report, label)
    if scorecard["complete"]:
        required_violations = set()
        grouped_expected: Counter[tuple[str, str, str]] = Counter()
        grouped_observed: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for result in report["scenario_results"]:
            key = (
                report.get("report_date", "historical"),
                result["run_id"],
                result["agent_id"],
            )
            grouped_expected[key] += result["expected_count"]
            grouped_observed[key].update(result["insight_references"])
        for key, expected_count in grouped_expected.items():
            observed_count = len(grouped_observed[key])
            if observed_count != expected_count:
                required_violations.add("finding_count_mismatch")
                required_violations.add(
                    "extra_noise" if observed_count > expected_count else "missing_findings"
                )
        if healthy_insights:
            required_violations.add("healthy_false_positive")
        if "collection_analysis" in report:
            for count_key, violation in (
                ("duplicates", "duplication"),
                ("fragments", "fragmentation"),
                ("umbrellas", "umbrella"),
                ("stale_version", "cross_version_stale"),
            ):
                if report["collection_analysis"][count_key]:
                    required_violations.add(violation)
        if rates["high_severity_recall"] != 1:
            required_violations.add("high_severity_recall")
        if rates["overall_recall"] < 0.90:
            required_violations.add("overall_recall")
        if rates["precision"] < 0.95:
            required_violations.add("precision")
        if (
            any(
                rates[name] != 1
                for name in (
                    "category_accuracy",
                    "severity_accuracy",
                    "title_pass_rate",
                    "description_pass_rate",
                    "proposed_fix_pass_rate",
                    "linked_trace_pass_rate",
                    "evidence_localization_rate",
                    "meaningfulness_rate",
                    "actionability_rate",
                )
                if name in rates
            )
            or rates.get("distinctness_rate", 1) != 1
        ):
            required_violations.add("attribute_correctness")
        missing_violations = required_violations - set(scorecard["violations"])
        if missing_violations:
            raise ContractError(
                f"{label}: scorecard omits derived gate violations: "
                + ", ".join(sorted(missing_violations))
            )
    if counts["active_scenarios"] != len(expected_ids):
        raise ContractError(
            f"{label}: scorecard active scenario count does not match the selected plan"
        )
    if counts["completed_scenarios"] != completed_count:
        raise ContractError(f"{label}: scorecard completed count does not match scenario results")
    expected_counts = {
        "true_positives": true_positives,
        "partially_useful": partially_useful,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "healthy_insights": healthy_insights,
    }
    for key, expected in expected_counts.items():
        if counts[key] != expected:
            raise ContractError(f"{label}: scorecard {key} does not match scenario results")
    if report.get("failure") is None:
        for key, expected in expected_rates.items():
            if not math.isclose(rates[key], expected, abs_tol=1e-9):
                raise ContractError(f"{label}: scorecard {key} does not match scenario results")
    if report["status"] == "INCONCLUSIVE":
        if scorecard["complete"]:
            raise ContractError(f"{label}: an INCONCLUSIVE report cannot be complete")
    elif trust_failures:
        raise ContractError(
            f"{label}: trust failures require INCONCLUSIVE status and incomplete scorecard"
        )
    elif not scorecard["complete"]:
        raise ContractError(f"{label}: a conclusive verdict requires a complete scorecard")

    if report["status"] == "AT BAR":
        required_one = {
            "high_severity_recall",
            "category_accuracy",
            "severity_accuracy",
            "title_pass_rate",
            "description_pass_rate",
            "proposed_fix_pass_rate",
            "linked_trace_pass_rate",
        }
        required_one.update(
            key
            for key in (
                "evidence_localization_rate",
                "meaningfulness_rate",
                "actionability_rate",
                "distinctness_rate",
            )
            if key in rates
        )
        required_zero = {"duplication_rate", "umbrella_rate", "cross_version_stale_rate"}
        required_zero.update(
            key for key in ("healthy_noise_rate", "fragmentation_rate") if key in rates
        )
        if (
            scorecard["violations"]
            or counts["healthy_insights"]
            or counts["false_positives"]
            or counts["false_negatives"]
            or counts["structural_failures"]
            or rates["overall_recall"] < 0.90
            or rates["precision"] < 0.95
            or any(rates[key] != 1 for key in required_one)
            or any(rates[key] != 0 for key in required_zero)
        ):
            raise ContractError(f"{label}: AT BAR does not satisfy every strict quality gate")
    elif report["status"] == "NOT AT BAR" and not scorecard["violations"]:
        raise ContractError(f"{label}: NOT AT BAR requires at least one recorded gate violation")


def validate_report_plan_binding(
    report: dict[str, Any],
    plan: dict[str, Any],
    label: str,
) -> None:
    from agent_insights_quality.reporting.model import (
        STRUCTURED_REPORT_VERSION,
        build_human_validation_checklists,
    )

    if (
        report["report_id"] != plan["plan_id"]
        or report["plan_id"] != plan["plan_id"]
        or report["report_date"] != plan["report_date"]
    ):
        raise ContractError(f"{label}: report identity does not match its daily plan")
    for key in ("build", "generator_model", "endpoint_reference"):
        if report["engine"][key] != plan["engine"][key]:
            raise ContractError(f"{label}: report engine {key} does not match its daily plan")
    assignment_by_scenario = {
        assignment["scenario_id"]: assignment for assignment in plan["assignments"]
    }
    result_ids = [result["scenario_id"] for result in report["scenario_results"]]
    if len(result_ids) != len(set(result_ids)):
        raise ContractError(f"{label}: scenario results must be unique")
    expected_ids = set(assignment_by_scenario)
    actual_ids = set(result_ids)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ContractError(
            f"{label}: scenario results must exactly match selected plan assignments; "
            f"missing={missing}, extra={extra}"
        )
    for result in report["scenario_results"]:
        assignment = assignment_by_scenario.get(result["scenario_id"])
        if assignment is None:
            raise ContractError(f"{label}: scenario result was not assigned by the daily plan")
        if result["agent_id"] != assignment["agent_id"]:
            raise ContractError(f"{label}: scenario result agent differs from the daily plan")
        if result["run_id"] != assignment["run_id"]:
            raise ContractError(f"{label}: scenario result run differs from the daily plan")
        current = assignment["version_sequence"][-1]
        if (
            result["version_sequence"]["phase"] != current["phase"]
            or result["version_sequence"]["version_digest"] != current["digest"]
            or result["agent_version_digest"] != current["digest"]
        ):
            raise ContractError(f"{label}: scenario result version differs from the daily plan")
    if report.get("schema_version") == STRUCTURED_REPORT_VERSION:
        expected_checklists = build_human_validation_checklists(report, plan)
        if report["human_validation_checklists"] != expected_checklists:
            raise ContractError(
                f"{label}: human validation checklists drift from the immutable daily plan"
            )


def validate_historical_report_semantics(
    report: dict[str, Any],
    plan: dict[str, Any],
    label: str,
) -> None:
    assignments = {item["scenario_id"]: item for item in plan["assignments"]}
    if len(assignments) != len(plan["assignments"]):
        raise ContractError(f"{label}: historical plan assignments must be unique")
    snapshot_agents = {
        assignment["agent_id"]: {
            "id": assignment["agent_id"],
            "agent_type": assignment["agent_type"],
            "required_name_prefix": assignment["agent_id"],
            "domain": "snapshot",
        }
        for assignment in plan["assignments"]
    }
    snapshot_scenarios = [
        {
            "id": assignment["scenario_id"],
            "status": "active",
            "compatibility": {
                "domains": ["snapshot"],
                "agent_types": [assignment["agent_type"]],
                "agent_ids": [assignment["agent_id"]],
            },
            "expected": assignment["expected"],
        }
        for assignment in plan["assignments"]
    ]
    validate_canonical_report_semantics(
        report,
        list(snapshot_agents.values()),
        {"scenarios": snapshot_scenarios},
        label,
        expected_scenario_ids=set(assignments),
    )


def validate_report_layout() -> None:
    reports_root = ROOT / "reports"
    allowed_root = {"latest.json", "latest.md", "trend.json"}
    daily_pattern = re.compile(
        r"^daily/(?P<year>[0-9]{4})/(?P<month>[0-9]{2})/(?P<day>[0-9]{2})/"
        r"(?:(?P<rerun>aiq-[0-9]{8}-r[0-9]{2})/)?"
        r"(?P<filename>plan\.json|plan\.md|report\.json|report\.md|failure-email\.html|"
        r"readiness-failure\.json|readiness-failure\.md|email-handoff\.json|"
        r"email-send-request\.json|email-receipt\.json|daily-status\.json)$"
    )
    files_by_record: dict[str, set[str]] = {}
    for path in sorted(reports_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(reports_root).as_posix()
        if relative == "daily/.gitkeep" or relative in allowed_root:
            continue
        match = daily_pattern.fullmatch(relative)
        if match is None:
            raise ContractError(f"reports/{relative}: unrecognized generated report path")
        year = match.group("year")
        month = match.group("month")
        day = match.group("day")
        rerun = match.group("rerun")
        filename = match.group("filename")
        try:
            date(int(year), int(month), int(day))
        except ValueError as error:
            raise ContractError(f"reports/{relative}: invalid report date path") from error
        if rerun is not None and not rerun.startswith(f"aiq-{year}{month}{day}-r"):
            raise ContractError(f"reports/{relative}: rerun plan ID does not match report date path")
        record = f"{year}/{month}/{day}" + (f"/{rerun}" if rerun else "")
        files_by_record.setdefault(record, set()).add(filename)
    complete_report = {"plan.json", "plan.md", "report.json", "report.md"}
    legacy_readiness_failure = {
        "readiness-failure.json",
        "readiness-failure.md",
        "failure-email.html",
        "email-handoff.json",
    }
    current_readiness_failure = {
        "readiness-failure.json",
        "readiness-failure.md",
        "failure-email.html",
        "email-send-request.json",
        "email-receipt.json",
    }
    readiness_markers = (
        legacy_readiness_failure | current_readiness_failure
    ) - {"failure-email.html"}
    operational_failure = complete_report | {"failure-email.html"}
    evidence_handoff = {"plan.json", "plan.md", "daily-status.json"}
    planned_run = {"plan.json", "plan.md"}
    legacy_readiness_records = {"2026/08/21"}
    for report_record, filenames in files_by_record.items():
        if filenames & readiness_markers:
            if (
                "/aiq-" in report_record
                or frozenset(filenames) not in {
                    frozenset(legacy_readiness_failure),
                    frozenset(current_readiness_failure),
                }
            ):
                raise ContractError(
                    f"reports/daily/{report_record}: readiness failure bundle must contain "
                    "exactly its reviewed legacy or receipt-validated artifacts at the date level"
                )
            if (
                frozenset(filenames) == frozenset(legacy_readiness_failure)
                and report_record not in legacy_readiness_records
            ):
                raise ContractError(
                    f"reports/daily/{report_record}: legacy email handoff is historical-only"
                )
        elif filenames not in (
            complete_report,
            complete_report | {"daily-status.json"},
            operational_failure,
            operational_failure | {"daily-status.json"},
            evidence_handoff,
            planned_run,
        ):
            raise ContractError(
                f"reports/daily/{report_record}: incomplete daily report artifact set"
            )


def validate_report_artifacts(
    agents: list[dict[str, Any]],
    catalog: dict[str, Any],
    reporting: dict[str, Any],
) -> None:
    validate_report_layout()
    plan_schema = SCHEMAS / "daily-plan.schema.json"
    report_schema = SCHEMAS / "canonical-report.schema.json"
    for path in sorted((ROOT / "reports" / "daily").glob("*/*/*/readiness-failure.json")):
        label = str(path.relative_to(ROOT))
        report = load_data(path)
        validate_instance(report, READINESS_FAILURE_SCHEMA, label)
        markdown = path.with_name("readiness-failure.md").read_text(encoding="ascii")
        email = path.with_name("failure-email.html").read_text(encoding="ascii")
        if (
            report["report_id"] not in markdown
            or report["report_date"] not in markdown
            or report["status"] not in markdown
        ):
            raise ContractError(f"{label}: readiness-failure.md omits canonical identity")
        if "INCONCLUSIVE" not in email:
            raise ContractError(f"{label}: failure email must state INCONCLUSIVE")
        if report["email_handoff_reference"] == "email-handoff.json":
            handoff = load_data(path.with_name("email-handoff.json"))
            validate_instance(handoff, EMAIL_HANDOFF_SCHEMA, f"{label}.email_handoff")
            if (
                handoff["report_id"] != report["report_id"]
                or handoff["report_date"] != report["report_date"]
            ):
                raise ContractError(
                    f"{label}: email handoff identity does not match failure report"
                )
            from agent_insights_quality.reporting import (
                validate_email_handoff,
                validate_stored_bundle_content,
            )

            validate_email_handoff(handoff, f"{label}.email_handoff", reporting)
            validate_stored_bundle_content(path.with_name("email-handoff.json"), handoff)
        else:
            from agent_insights_quality.artifact_io import verified_hash
            from agent_insights_quality.reporting import import_email_receipt

            request = load_data(path.with_name("email-send-request.json"))
            receipt = load_data(path.with_name("email-receipt.json"))
            validate_instance(
                request,
                SCHEMAS / "email-send-request.schema.json",
                f"{label}.email_send_request",
            )
            verified_hash(
                request,
                "request_hash",
                f"{label}.email_send_request",
            )
            if request["html"] != email or report["report_date"] not in request["subject"]:
                raise ContractError(
                    f"{label}: readiness email request does not match rendered failure email"
                )
            import_email_receipt(request, receipt)
    for path in sorted((ROOT / "reports" / "daily").rglob("plan.json")):
        label = str(path.relative_to(ROOT))
        plan = load_data(path)
        validate_instance(plan, plan_schema, label)
        validate_daily_plan_semantics(
            plan,
            agents,
            catalog,
            label,
            allow_historical=True,
        )
        if path.parent.relative_to(ROOT).as_posix() != plan["artifact_directory"]:
            raise ContractError(f"{label}: plan is stored outside its artifact directory")
        markdown = path.with_name("plan.md").read_text(encoding="ascii")
        if plan["plan_id"] not in markdown or plan["report_date"] not in markdown:
            raise ContractError(f"{label}: plan.md does not identify its canonical plan")
        status_path = path.with_name("daily-status.json")
        if status_path.is_file():
            from agent_insights_quality.daily import validate_daily_status

            validate_daily_status(
                load_data(status_path),
                plan,
            )
    for path in sorted((ROOT / "reports" / "daily").rglob("report.json")):
        label = str(path.relative_to(ROOT))
        report = load_data(path)
        validate_instance(report, report_schema, label)
        validate_instance(report["scorecard"], SCORECARD_SCHEMA, f"{label}.scorecard")
        markdown = path.with_name("report.md").read_text(encoding="ascii")
        if (
            report["report_id"] not in markdown
            or report["report_date"] not in markdown
            or report["status"] not in markdown
        ):
            raise ContractError(f"{label}: report.md contradicts or omits canonical report identity")
        plan_path = path.with_name("plan.json")
        if not plan_path.is_file():
            raise ContractError(f"{label}: report requires a sibling plan.json")
        plan = load_data(plan_path)
        validate_report_plan_binding(report, plan, label)
        if plan["catalog_version"] == catalog["catalog_version"]:
            validate_canonical_report_semantics(
                report,
                agents,
                catalog,
                label,
                expected_scenario_ids={
                    assignment["scenario_id"] for assignment in plan["assignments"]
                },
            )
        else:
            validate_historical_report_semantics(report, plan, label)
        failure_email = path.with_name("failure-email.html")
        if failure_email.exists():
            if report["status"] != "INCONCLUSIVE":
                raise ContractError(f"{label}: failure email is valid only for INCONCLUSIVE reports")
            if "INCONCLUSIVE" not in failure_email.read_text(encoding="ascii"):
                raise ContractError(f"{label}: failure email must state INCONCLUSIVE")

    latest_json = ROOT / "reports" / "latest.json"
    latest_markdown = ROOT / "reports" / "latest.md"
    if latest_json.exists() != latest_markdown.exists():
        raise ContractError("reports/latest.json and reports/latest.md must be updated together")
    if latest_json.exists():
        latest = load_data(latest_json)
        validate_instance(latest, report_schema, "reports/latest.json")
        validate_instance(latest["scorecard"], SCORECARD_SCHEMA, "reports/latest.json.scorecard")
        day_root = (
            ROOT
            / "reports"
            / "daily"
            / latest["report_date"].replace("-", "/")
        )
        base_report_id = f"aiq-{latest['report_date'].replace('-', '')}"
        if latest["report_id"] != base_report_id:
            day_root /= latest["report_id"]
        daily_json = day_root / "report.json"
        daily_markdown = day_root / "report.md"
        plan_path = day_root / "plan.json"
        if not daily_json.is_file() or load_data(daily_json) != latest:
            raise ContractError("reports/latest.json must exactly match its dated report.json")
        if not daily_markdown.is_file() or daily_markdown.read_bytes() != latest_markdown.read_bytes():
            raise ContractError("reports/latest.md must exactly match its dated report.md")
        if not plan_path.is_file():
            raise ContractError("reports/latest.json requires its dated plan.json")
        latest_plan = load_data(plan_path)
        validate_report_plan_binding(
            latest,
            latest_plan,
            "reports/latest.json",
        )
        if latest_plan["catalog_version"] == catalog["catalog_version"]:
            validate_canonical_report_semantics(
                latest,
                agents,
                catalog,
                "reports/latest.json",
                expected_scenario_ids={
                    assignment["scenario_id"]
                    for assignment in latest_plan["assignments"]
                },
            )
        else:
            validate_historical_report_semantics(
                latest,
                latest_plan,
                "reports/latest.json",
            )

    trend_path = ROOT / "reports" / "trend.json"
    if trend_path.exists():
        validate_instance(
            load_data(trend_path),
            SCHEMAS / "trend.schema.json",
            "reports/trend.json",
        )


def validate_contracts() -> None:
    from agent_insights_quality.readiness import validate_runtime_readiness
    from agent_insights_quality.healthy_agents import load_healthy_agents

    validate_structured_file_syntax()
    validate_schemas()
    agents = load_agent_manifests()
    load_healthy_agents()
    catalog = load_scenario_catalog({agent["id"] for agent in agents})
    validate_supporting_manifests(catalog)
    load_selection_policy(catalog)
    validate_instance(
        load_data(ROOT / "state" / "quality-memory.json"),
        MEMORY_SCHEMA,
        "state/quality-memory.json",
    )
    reporting = load_data(ROOT / "config" / "reporting.yaml")
    validate_reporting_config(reporting)
    validate_ado_policy(load_data(ROOT / "config" / "ado-policy.yaml"))
    validate_automation_policy(load_data(ROOT / "config" / "automation-policy.yaml"))
    validate_security_policy(load_data(ROOT / "config" / "security-policy.yaml"))
    validate_traffic_policy(load_data(ROOT / "config" / "traffic-policy.yaml"))
    validate_link_policy(load_data(ROOT / "config" / "link-policy.yaml"))
    validate_runtime_readiness(load_data(ROOT / "config" / "runtime-readiness.yaml"))
    validate_report_artifacts(agents, catalog, reporting)
