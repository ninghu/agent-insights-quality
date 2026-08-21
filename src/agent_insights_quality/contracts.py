from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
AGENT_SCHEMA = SCHEMAS / "agent-manifest.schema.json"
SCENARIO_SCHEMA = SCHEMAS / "scenario-manifest.schema.json"
MEMORY_SCHEMA = SCHEMAS / "quality-memory.schema.json"
SCORECARD_SCHEMA = SCHEMAS / "scorecard.schema.json"
READINESS_FAILURE_SCHEMA = SCHEMAS / "readiness-failure.schema.json"
EMAIL_HANDOFF_SCHEMA = SCHEMAS / "email-handoff.schema.json"

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


class ContractError(ValueError):
    """Raised when a repository contract is invalid."""


def load_data(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix == ".json":
            return json.load(handle)
        return yaml.safe_load(handle)


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
    scenario_ids = [scenario["id"] for scenario in catalog["scenarios"]]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ContractError("Scenario IDs must be unique")

    for scenario in catalog["scenarios"]:
        mutation = scenario["mutation"]["manifest"]
        if scenario["mutation"]["kind"] == "none" and mutation is not None:
            raise ContractError(f"{scenario['id']}: a no-mutation scenario must not name a mutation manifest")
        if scenario["mutation"]["kind"] != "none" and mutation is None:
            raise ContractError(f"{scenario['id']}: a fault scenario must name a mutation manifest")
        for relative in filter(None, [mutation, scenario["traffic"]["recipe"]]):
            if not (ROOT / relative).is_file():
                raise ContractError(f"{scenario['id']}: referenced file does not exist: {relative}")
        explicit_agents = set(scenario["compatibility"]["agent_ids"])
        if agent_ids is not None and not explicit_agents.issubset(agent_ids):
            raise ContractError(f"{scenario['id']}: compatibility references unknown agents")
    return catalog


def _require_exact_keys(data: Any, required: set[str], label: str) -> None:
    if not isinstance(data, dict):
        raise ContractError(f"{label}: expected an object")
    actual = set(data)
    if actual != required:
        raise ContractError(f"{label}: expected keys {sorted(required)}, found {sorted(actual)}")


def validate_supporting_manifests(catalog: dict[str, Any]) -> None:
    scenarios = {scenario["id"] for scenario in catalog["scenarios"]}
    mutation_keys = {"schema_version", "scenario_id", "status", "description"}
    traffic_keys = mutation_keys | {"endpoint_only"}
    for path in sorted((ROOT / "scenarios" / "mutations").glob("*.yaml")):
        data = load_data(path)
        _require_exact_keys(data, mutation_keys, str(path.relative_to(ROOT)))
        if data["schema_version"] != "1.0.0" or data["status"] != "placeholder":
            raise ContractError(f"{path.relative_to(ROOT)}: invalid placeholder metadata")
        if data["scenario_id"] not in scenarios:
            raise ContractError(f"{path.relative_to(ROOT)}: unknown scenario_id")
    for path in sorted((ROOT / "scenarios" / "traffic").glob("*.yaml")):
        data = load_data(path)
        _require_exact_keys(data, traffic_keys, str(path.relative_to(ROOT)))
        if data["schema_version"] != "1.0.0" or data["status"] != "placeholder":
            raise ContractError(f"{path.relative_to(ROOT)}: invalid placeholder metadata")
        if data["scenario_id"] not in scenarios:
            raise ContractError(f"{path.relative_to(ROOT)}: unknown scenario_id")
        if data["endpoint_only"] is not True:
            raise ContractError(f"{path.relative_to(ROOT)}: endpoint_only must be true")


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


def _catalog_hash() -> str:
    content = (ROOT / "scenarios" / "catalog.yaml").read_bytes()
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def validate_daily_plan_semantics(
    plan: dict[str, Any],
    agents: list[dict[str, Any]],
    catalog: dict[str, Any],
    label: str,
) -> None:
    agent_by_id = {agent["id"]: agent for agent in agents}
    scenario_by_id = {scenario["id"]: scenario for scenario in catalog["scenarios"]}
    active_ids = {
        scenario["id"] for scenario in catalog["scenarios"] if scenario["status"] == "active"
    }
    assignment_ids = [assignment["scenario_id"] for assignment in plan["assignments"]]
    if plan["catalog_version"] != catalog["catalog_version"]:
        raise ContractError(f"{label}: catalog_version does not match scenarios/catalog.yaml")
    if plan["catalog_hash"] != _catalog_hash():
        raise ContractError(f"{label}: catalog_hash does not match scenarios/catalog.yaml")
    if len(assignment_ids) != len(set(assignment_ids)):
        raise ContractError(f"{label}: each scenario may be assigned only once")
    if set(assignment_ids) != active_ids:
        raise ContractError(f"{label}: assignments must cover every active scenario exactly once")

    for assignment in plan["assignments"]:
        scenario = scenario_by_id.get(assignment["scenario_id"])
        agent = agent_by_id.get(assignment["agent_id"])
        if scenario is None:
            raise ContractError(f"{label}: assignment references an unknown scenario")
        if agent is None:
            raise ContractError(f"{label}: assignment references an unknown agent")
        if assignment["scenario_version"] != scenario["version"]:
            raise ContractError(f"{label}: scenario version does not match the catalog")
        if not assignment["agent_name"].startswith(agent["required_name_prefix"]):
            raise ContractError(f"{label}: agent name does not use its required prefix")
        compatibility = scenario["compatibility"]
        if agent["domain"] not in compatibility["domains"]:
            raise ContractError(f"{label}: scenario is incompatible with the assigned agent domain")
        if agent["agent_type"] not in compatibility["agent_types"]:
            raise ContractError(f"{label}: scenario is incompatible with the assigned agent type")
        if compatibility["agent_ids"] and agent["id"] not in compatibility["agent_ids"]:
            raise ContractError(f"{label}: scenario is incompatible with the assigned agent ID")


def validate_canonical_report_semantics(
    report: dict[str, Any],
    agents: list[dict[str, Any]],
    catalog: dict[str, Any],
    label: str,
) -> None:
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
    active_ids = {
        scenario["id"] for scenario in catalog["scenarios"] if scenario["status"] == "active"
    }
    result_ids = [result["scenario_id"] for result in report["scenario_results"]]
    if len(result_ids) != len(set(result_ids)):
        raise ContractError(f"{label}: scenario results must be unique")
    if not set(result_ids).issubset(active_ids):
        raise ContractError(f"{label}: report contains a non-active or unknown scenario")
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
        expected_fault = scenario["expected"]["category"] != "none"
        references = result["insight_references"]
        if result["verdict"] == "correct" and len(references) != int(expected_fault):
            raise ContractError(f"{label}: correct result has an inconsistent insight reference count")
        if result["verdict"] in {"partially_useful", "incorrect_noise"} and not references:
            raise ContractError(f"{label}: produced insight verdict requires an opaque reference")
        if result["verdict"] in {"missed", "inconclusive"} and references:
            raise ContractError(f"{label}: missing/inconclusive result cannot reference an insight")

    scorecard = report["scorecard"]
    counts = scorecard["counts"]
    rates = scorecard["rates"]
    completed_count = sum(result["completed"] for result in report["scenario_results"])
    complete = (
        set(result_ids) == active_ids
        and completed_count == len(active_ids)
        and all(result["verdict"] != "inconclusive" for result in report["scenario_results"])
    )
    fault_results = [
        result
        for result in report["scenario_results"]
        if scenario_by_id[result["scenario_id"]]["expected"]["category"] != "none"
    ]
    true_positives = sum(result["verdict"] == "correct" for result in fault_results)
    partially_useful = sum(
        result["verdict"] == "partially_useful" for result in report["scenario_results"]
    )
    false_negatives = sum(result["verdict"] != "correct" for result in fault_results)
    produced_insights = sum(
        len(result["insight_references"]) for result in report["scenario_results"]
    )
    false_positives = produced_insights - true_positives
    healthy_insights = sum(
        len(result["insight_references"])
        for result in report["scenario_results"]
        if scenario_by_id[result["scenario_id"]]["expected"]["category"] == "none"
    )
    expected_high = [
        result
        for result in fault_results
        if scenario_by_id[result["scenario_id"]]["expected"]["severity"] == "high"
    ]

    def ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 1.0

    expected_rates = {
        "high_severity_recall": ratio(
            sum(result["verdict"] == "correct" for result in expected_high),
            len(expected_high),
        ),
        "overall_recall": ratio(true_positives, len(fault_results)),
        "precision": ratio(true_positives, produced_insights),
    }
    expected_rates["f1"] = ratio(
        2 * expected_rates["precision"] * expected_rates["overall_recall"],
        expected_rates["precision"] + expected_rates["overall_recall"],
    )

    if report["status"] != scorecard["verdict"]:
        raise ContractError(f"{label}: report status and scorecard verdict must match")
    if counts["active_scenarios"] != len(active_ids):
        raise ContractError(f"{label}: scorecard active scenario count does not match the catalog")
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
    for key, expected in expected_rates.items():
        if not math.isclose(rates[key], expected, abs_tol=1e-9):
            raise ContractError(f"{label}: scorecard {key} does not match scenario results")
    if scorecard["complete"] != complete:
        raise ContractError(f"{label}: scorecard completeness does not match scenario results")

    if report["status"] == "INCONCLUSIVE":
        if scorecard["complete"]:
            raise ContractError(f"{label}: an INCONCLUSIVE report cannot be complete")
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
        required_zero = {"duplication_rate", "umbrella_rate", "cross_version_stale_rate"}
        if (
            scorecard["violations"]
            or counts["healthy_insights"]
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
    for result in report["scenario_results"]:
        assignment = assignment_by_scenario.get(result["scenario_id"])
        if assignment is None:
            raise ContractError(f"{label}: scenario result was not assigned by the daily plan")
        if result["agent_id"] != assignment["agent_id"]:
            raise ContractError(f"{label}: scenario result agent differs from the daily plan")
        if result["agent_version_digest"] != assignment["agent_version_digest"]:
            raise ContractError(f"{label}: scenario result version differs from the daily plan")


def validate_report_layout() -> None:
    reports_root = ROOT / "reports"
    allowed_root = {"latest.json", "latest.md", "trend.json"}
    daily_pattern = re.compile(
        r"^daily/([0-9]{4})/([0-9]{2})/([0-9]{2})/"
        r"(plan\.json|plan\.md|report\.json|report\.md|failure-email\.html|"
        r"readiness-failure\.json|readiness-failure\.md|email-handoff\.json)$"
    )
    files_by_day: dict[str, set[str]] = {}
    for path in sorted(reports_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(reports_root).as_posix()
        if relative == "daily/.gitkeep" or relative in allowed_root:
            continue
        match = daily_pattern.fullmatch(relative)
        if match is None:
            raise ContractError(f"reports/{relative}: unrecognized generated report path")
        year, month, day, filename = match.groups()
        try:
            date(int(year), int(month), int(day))
        except ValueError as error:
            raise ContractError(f"reports/{relative}: invalid report date path") from error
        files_by_day.setdefault(f"{year}/{month}/{day}", set()).add(filename)
    complete_report = {"plan.json", "plan.md", "report.json", "report.md"}
    readiness_failure = {
        "readiness-failure.json",
        "readiness-failure.md",
        "failure-email.html",
        "email-handoff.json",
    }
    readiness_markers = readiness_failure - {"failure-email.html"}
    operational_failure = complete_report | {"failure-email.html"}
    for report_day, filenames in files_by_day.items():
        if filenames & readiness_markers:
            if filenames != readiness_failure:
                raise ContractError(
                    f"reports/daily/{report_day}: readiness failure bundle must contain "
                    "exactly its four artifacts"
                )
        elif filenames not in (complete_report, operational_failure):
            raise ContractError(f"reports/daily/{report_day}: incomplete daily report artifact set")


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
        handoff = load_data(path.with_name("email-handoff.json"))
        validate_instance(handoff, EMAIL_HANDOFF_SCHEMA, f"{label}.email_handoff")
        if (
            report["report_id"] not in markdown
            or report["report_date"] not in markdown
            or report["status"] not in markdown
        ):
            raise ContractError(f"{label}: readiness-failure.md omits canonical identity")
        if "INCONCLUSIVE" not in email:
            raise ContractError(f"{label}: failure email must state INCONCLUSIVE")
        if handoff["report_id"] != report["report_id"] or handoff["report_date"] != report["report_date"]:
            raise ContractError(f"{label}: email handoff identity does not match failure report")
        from agent_insights_quality.reporting import validate_email_handoff

        validate_email_handoff(handoff, f"{label}.email_handoff", reporting)
        from agent_insights_quality.reporting import validate_stored_bundle_content

        validate_stored_bundle_content(path.with_name("email-handoff.json"), handoff)
    for path in sorted((ROOT / "reports" / "daily").glob("*/*/*/plan.json")):
        label = str(path.relative_to(ROOT))
        plan = load_data(path)
        validate_instance(plan, plan_schema, label)
        validate_daily_plan_semantics(plan, agents, catalog, label)
        markdown = path.with_name("plan.md").read_text(encoding="ascii")
        if plan["plan_id"] not in markdown or plan["report_date"] not in markdown:
            raise ContractError(f"{label}: plan.md does not identify its canonical plan")
    for path in sorted((ROOT / "reports" / "daily").glob("*/*/*/report.json")):
        label = str(path.relative_to(ROOT))
        report = load_data(path)
        validate_instance(report, report_schema, label)
        validate_instance(report["scorecard"], SCORECARD_SCHEMA, f"{label}.scorecard")
        validate_canonical_report_semantics(report, agents, catalog, label)
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
        validate_canonical_report_semantics(latest, agents, catalog, "reports/latest.json")
        day_root = (
            ROOT
            / "reports"
            / "daily"
            / latest["report_date"].replace("-", "/")
        )
        daily_json = day_root / "report.json"
        daily_markdown = day_root / "report.md"
        if not daily_json.is_file() or load_data(daily_json) != latest:
            raise ContractError("reports/latest.json must exactly match its dated report.json")
        if not daily_markdown.is_file() or daily_markdown.read_bytes() != latest_markdown.read_bytes():
            raise ContractError("reports/latest.md must exactly match its dated report.md")

    trend_path = ROOT / "reports" / "trend.json"
    if trend_path.exists():
        validate_instance(
            load_data(trend_path),
            SCHEMAS / "trend.schema.json",
            "reports/trend.json",
        )


def validate_contracts() -> None:
    from agent_insights_quality.readiness import validate_runtime_readiness

    validate_structured_file_syntax()
    validate_schemas()
    agents = load_agent_manifests()
    catalog = load_scenario_catalog({agent["id"] for agent in agents})
    validate_supporting_manifests(catalog)
    validate_instance(
        load_data(ROOT / "state" / "quality-memory.json"),
        MEMORY_SCHEMA,
        "state/quality-memory.json",
    )
    reporting = load_data(ROOT / "config" / "reporting.yaml")
    validate_reporting_config(reporting)
    validate_automation_policy(load_data(ROOT / "config" / "automation-policy.yaml"))
    validate_security_policy(load_data(ROOT / "config" / "security-policy.yaml"))
    validate_traffic_policy(load_data(ROOT / "config" / "traffic-policy.yaml"))
    validate_link_policy(load_data(ROOT / "config" / "link-policy.yaml"))
    validate_runtime_readiness(load_data(ROOT / "config" / "runtime-readiness.yaml"))
    validate_report_artifacts(agents, catalog, reporting)
