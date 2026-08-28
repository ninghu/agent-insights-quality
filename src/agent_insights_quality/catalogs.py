from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from agent_insights_quality.util import (
    ROOT,
    ContractError,
    content_hash,
    file_hash,
    read_json,
    read_yaml,
)

AGENT_CATALOG_PATH = ROOT / "catalogs" / "AGENT_CATALOG.yaml"
ISSUE_CATALOG_PATH = ROOT / "catalogs" / "ISSUE_CATALOG.yaml"
AGENT_SCHEMA_PATH = ROOT / "schemas" / "agent-catalog.schema.json"
ISSUE_SCHEMA_PATH = ROOT / "schemas" / "issue-catalog.schema.json"
PROMPT_DEFINITION_SCHEMA_PATH = ROOT / "schemas" / "prompt-definition.schema.json"
PROMPT_TRAFFIC_SCHEMA_PATH = ROOT / "schemas" / "prompt-traffic.schema.json"

EXPECTED_ASSIGNMENTS = {
    "weather-agent": 6,
    "healthcare-agent": 6,
    "finance-agent": 8,
    "travel-agent": 8,
    "support-ticket-agent": 8,
}


def _validate_schema(value: dict[str, Any], schema_path: Path, label: str) -> None:
    schema = read_json(schema_path)
    _validate_value_schema(value, schema, label)


def _validate_value_schema(
    value: dict[str, Any],
    schema: dict[str, Any],
    label: str,
) -> None:
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ContractError(f"{label} schema error at {location}: {error.message}")


def _validate_prompt_definition(value: dict[str, Any], label: str) -> None:
    definition = value.get("definition")
    if isinstance(definition, dict) and "tools" in definition:
        raise ContractError(f"{label} pure Prompt definition cannot contain tools")
    _validate_schema(value, PROMPT_DEFINITION_SCHEMA_PATH, label)


def _validate_prompt_traffic(
    value: dict[str, Any],
    label: str,
    *,
    require_activation: bool,
    require_all_assertions: bool,
) -> None:
    requests = value.get("requests")
    if isinstance(requests, list) and any(
        isinstance(item, dict) and "tool_fixtures" in item for item in requests
    ):
        raise ContractError(f"{label} pure Prompt traffic cannot contain tool fixtures")
    _validate_schema(value, PROMPT_TRAFFIC_SCHEMA_PATH, label)
    if require_all_assertions and any(
        not isinstance(item.get("expected"), dict)
        or not item["expected"].get("semantic_assertions")
        for item in value["requests"]
    ):
        raise ContractError(f"{label} requires assertions for every request")
    for item in value["requests"]:
        expected = item.get("expected")
        assertions = (
            expected.get("semantic_assertions")
            if isinstance(expected, dict)
            else None
        )
        schema = assertions.get("json_schema") if isinstance(assertions, dict) else None
        if schema is None:
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise ContractError(
                f"{label} contains an invalid assertion JSON schema"
            ) from error
    if require_activation and not any(
        isinstance(item, dict)
        and isinstance(item.get("expected"), dict)
        and item["expected"].get("activation_gate") is True
        for item in value["requests"]
    ):
        raise ContractError(f"{label} requires an issue activation gate")


def _validate_prompt_issue_delta(
    baseline: dict[str, Any],
    issue: dict[str, Any],
    label: str,
) -> None:
    baseline_instructions = baseline["definition"]["instructions"].rstrip()
    issue_instructions = issue["definition"]["instructions"]
    if not issue_instructions.startswith(baseline_instructions + "\n"):
        raise ContractError(
            f"{label} instructions must append one defect to the healthy baseline"
        )
    normalized = copy.deepcopy(issue)
    normalized["definition"]["instructions"] = baseline["definition"]["instructions"]
    normalized["metadata"]["logical_version"] = baseline["metadata"][
        "logical_version"
    ]
    if normalized != baseline:
        raise ContractError(
            f"{label} Prompt definition differs outside the reviewed defect paths"
        )


def load_catalogs(*, require_paths: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    agents = read_yaml(AGENT_CATALOG_PATH)
    issues = read_yaml(ISSUE_CATALOG_PATH)
    _validate_schema(agents, AGENT_SCHEMA_PATH, "Agent Catalog")
    _validate_schema(issues, ISSUE_SCHEMA_PATH, "Issue Catalog")
    validate_semantics(agents, issues, require_paths=require_paths)
    return agents, issues


def validate_semantics(
    agents: dict[str, Any],
    issues: dict[str, Any],
    *,
    require_paths: bool,
) -> None:
    if agents.get("models", {}).get("test_agents") != {
        "name": "GPT-5.4 mini",
        "id": "gpt-5.4-mini",
        "version": "2026-03-17",
        "deployment_role": "test-agent",
    }:
        raise ContractError("Test Agent model contract must use GPT-5.4 mini")
    agent_items = agents["agents"]
    issue_items = issues["issues"]
    delta_contracts = issues.get("source_delta_contracts")
    if not isinstance(delta_contracts, dict):
        raise ContractError("Issue Catalog source delta contracts are missing")
    by_agent = {item["name"]: item for item in agent_items}
    if set(by_agent) != set(EXPECTED_ASSIGNMENTS):
        raise ContractError("Agent Catalog must contain the five fixed Agents")
    terminal_modes = {
        "weather-agent": "direct_prompt",
        "healthcare-agent": "direct_prompt",
        "finance-agent": "standard_assistant_message",
        "travel-agent": "standard_assistant_message",
        "support-ticket-agent": "explicit_span_attributes",
    }
    if any(
        agent["baseline_contract"]["terminal_response"]
        != terminal_modes[agent_name]
        for agent_name, agent in by_agent.items()
    ):
        raise ContractError("Agent baseline terminal-evidence modes are not reviewed")

    ids = [item["id"] for item in issue_items]
    expected_ids = [f"issue-{number:03d}" for number in range(1, 37)]
    if ids != expected_ids:
        raise ContractError("Issue IDs must be ordered and continuous from issue-001 to issue-036")
    if len(ids) != len(set(ids)):
        raise ContractError("Issue IDs must be unique")

    counts = Counter(item["agent"] for item in issue_items)
    if dict(counts) != EXPECTED_ASSIGNMENTS:
        raise ContractError("Issue assignments do not match the reviewed fixed distribution")
    expected_delta_ids = set(ids)
    if set(delta_contracts) != expected_delta_ids:
        raise ContractError(
            "Source delta contracts must cover every reviewed issue"
        )

    assigned_ids: set[str] = set()
    for agent_name, agent in by_agent.items():
        expected = [
            item["id"] for item in issue_items if item["agent"] == agent_name
        ]
        if agent["issue_ids"] != expected:
            raise ContractError(f"{agent_name} issue_ids do not match the Issue Catalog")
        assigned_ids.update(agent["issue_ids"])
        if require_paths and not (ROOT / agent["baseline_path"]).is_dir():
            raise ContractError(f"{agent_name} baseline path is missing")
        if require_paths:
            _validate_baseline(agent)
    if assigned_ids != set(ids):
        raise ContractError("Every issue must be assigned exactly once")

    for issue in issue_items:
        expected_path = f"agents/{issue['agent']}/issues/{issue['id']}"
        if issue["implementation"] != expected_path:
            raise ContractError(f"{issue['id']} implementation path is not canonical")
        if require_paths:
            path = ROOT / expected_path
            if not path.is_dir():
                raise ContractError(f"{issue['id']} implementation folder is missing")
            for required in ("implementation.yaml", "traffic.json"):
                if not (path / required).is_file():
                    raise ContractError(f"{issue['id']} is missing {required}")
            _validate_implementation(issue, path, by_agent[issue["agent"]])
            _validate_source_delta(
                issue,
                path,
                by_agent[issue["agent"]],
                delta_contracts.get(issue["id"]),
            )


def catalog_hashes(
    agents: dict[str, Any],
    issues: dict[str, Any],
) -> dict[str, str]:
    files = {
        path.relative_to(ROOT).as_posix(): file_hash(path)
        for path in sorted((ROOT / "agents").rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.lower() not in {".pyc", ".zip"}
    }
    return {
        "agents": content_hash(agents),
        "issues": content_hash(issues),
        "artifacts": content_hash(files),
    }


def agent_model_contract(agents: dict[str, Any]) -> dict[str, str]:
    model = agents["models"]["test_agents"]
    return {
        "deployment_name": str(model["id"]),
        "model_id": str(model["id"]),
        "model_version": str(model["version"]),
    }


def _validate_baseline(agent: dict[str, Any]) -> None:
    root = ROOT / agent["baseline_path"]
    metadata = read_yaml(root / "implementation.yaml")
    if metadata.get("issue_id") != "v0" or metadata.get("agent_name") != agent["name"]:
        raise ContractError(f"{agent['name']} baseline metadata is invalid")
    traffic = json.loads((root / "traffic.json").read_text(encoding="utf-8"))
    requests = traffic.get("requests") if isinstance(traffic, dict) else None
    if not isinstance(requests, list) or len(requests) < 5:
        raise ContractError(f"{agent['name']} baseline requires at least five requests")
    if len(requests) != int(agent["baseline_contract"]["request_count"]):
        raise ContractError(
            f"{agent['name']} baseline request count does not match its contract"
        )
    if agent["type"] == "prompt":
        definition = json.loads((root / "definition.json").read_text(encoding="utf-8"))
        _validate_prompt_definition(definition, f"{agent['name']} baseline")
        _validate_prompt_traffic(
            traffic,
            f"{agent['name']} baseline",
            require_activation=False,
            require_all_assertions=True,
        )
        if definition.get("definition", {}).get("model") != "gpt-5.4-mini":
            raise ContractError(f"{agent['name']} Prompt definition must use GPT-5.4 mini")


def _validate_implementation(
    issue: dict[str, Any],
    root: Path,
    agent: dict[str, Any],
) -> None:
    metadata = read_yaml(root / "implementation.yaml")
    for key, expected in (
        ("issue_id", issue["id"]),
        ("agent_name", issue["agent"]),
        ("category", issue["category"]),
        ("severity", issue["severity"]),
    ):
        if metadata.get(key) != expected:
            raise ContractError(f"{issue['id']} implementation {key} does not match")
    if metadata.get("injected_defect", {}).get("single_root") in {None, ""}:
        raise ContractError(f"{issue['id']} must declare one injected root")
    if agent["type"] == "prompt":
        definition_path = root / "definition.json"
        if not definition_path.is_file():
            raise ContractError(
                f"{issue['id']} Prompt source definition is missing"
            )
        value = json.loads(definition_path.read_text(encoding="utf-8"))
        _validate_prompt_definition(value, f"{issue['id']} definition")
        declared_definition_hash = (
            metadata.get("determinism", {}).get("definition_sha256")
        )
        if declared_definition_hash != file_hash(definition_path).removeprefix(
            "sha256:"
        ):
            raise ContractError(
                f"{issue['id']} Prompt definition digest is stale"
            )
        if (
            value.get("name") != agent["name"]
            or value.get("definition", {}).get("kind") != "prompt"
            or value.get("definition", {}).get("model") != "gpt-5.4-mini"
            or value.get("metadata", {}).get("logical_version") != issue["id"]
        ):
            raise ContractError(
                f"{issue['id']} Prompt source definition is not self-contained"
            )
        baseline_definition = read_json(
            ROOT / agent["baseline_path"] / "definition.json"
        )
        _validate_prompt_issue_delta(
            baseline_definition,
            value,
            issue["id"],
        )
    else:
        baseline_source = ROOT / agent["baseline_path"] / "source"
        issue_source = root / "source"
        baseline_files = {
            path.relative_to(baseline_source).as_posix()
            for path in baseline_source.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        issue_files = {
            path.relative_to(issue_source).as_posix()
            for path in issue_source.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        if not baseline_files or issue_files != baseline_files:
            raise ContractError(
                f"{issue['id']} Hosted source tree is not self-contained"
            )
    traffic = json.loads((root / "traffic.json").read_text(encoding="utf-8"))
    if agent["type"] == "prompt":
        _validate_prompt_traffic(
            traffic,
            f"{issue['id']} traffic",
            require_activation=True,
            require_all_assertions=False,
        )
    requests = traffic.get("requests") if isinstance(traffic, dict) else None
    if (
        not isinstance(requests, list)
        or len(requests) < issue["trace_contract"]["minimum_traces"]
        or len(requests) < 5
    ):
        raise ContractError(f"{issue['id']} has insufficient endpoint traffic")
    for request in requests:
        remote = request.get("request") if isinstance(request, dict) else None
        if (
            not isinstance(remote, dict)
            or remote.get("method") != "POST"
            or remote.get("path") != "/responses"
            or not isinstance(remote.get("body"), dict)
            or "input" not in remote["body"]
        ):
            raise ContractError(f"{issue['id']} contains invalid endpoint traffic")
    if issue["id"] == "issue-005":
        conversations = Counter(
            str(
                request["request"]["body"].get("conversation", {}).get("id", "")
            )
            for request in requests
        )
        if (
            len(requests) != 10
            or len(conversations) != 5
            or "" in conversations
            or set(conversations.values()) != {2}
            or not all(
                request["expected"].get("activation_gate") is True
                for request in requests
            )
        ):
            raise ContractError(
                "issue-005 requires five independent two-turn activation groups"
            )


def _validate_source_delta(
    issue: dict[str, Any],
    root: Path,
    agent: dict[str, Any],
    contract: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(contract, dict):
        raise ContractError(f"{issue['id']} source delta contract is missing")
    if agent["type"] == "prompt":
        if contract != {
            "authority": "prompt_definition",
            "changed_paths": [
                "definition.instructions",
                "metadata.logical_version",
            ],
        }:
            raise ContractError(f"{issue['id']} Prompt source delta is not reviewed")
        baseline_path = ROOT / agent["baseline_path"] / "definition.json"
        issue_path = root / "definition.json"
        return {
            "authority": "prompt_definition",
            "changed_paths": contract["changed_paths"],
            "baseline_digest": file_hash(baseline_path),
            "issue_digest": file_hash(issue_path),
        }
    if contract.get("authority") != "hosted_source":
        raise ContractError(f"{issue['id']} Hosted source delta is not reviewed")
    baseline_source = ROOT / agent["baseline_path"] / "source"
    issue_source = root / "source"
    baseline_files = {
        path.relative_to(baseline_source).as_posix(): path
        for path in baseline_source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    issue_files = {
        path.relative_to(issue_source).as_posix(): path
        for path in issue_source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if set(issue_files) != set(baseline_files):
        raise ContractError(
            f"{issue['id']} Hosted source tree is not self-contained"
        )
    changed_files = sorted(
        name
        for name in baseline_files
        if baseline_files[name].read_bytes() != issue_files[name].read_bytes()
    )
    if changed_files != sorted(contract.get("changed_files", [])):
        raise ContractError(
            f"{issue['id']} source differs outside its reviewed delta manifest"
        )
    return {
        "authority": "hosted_source",
        "changed_files": changed_files,
        "baseline_tree_digest": content_hash(
            {
                name: file_hash(path)
                for name, path in sorted(baseline_files.items())
            }
        ),
        "issue_tree_digest": content_hash(
            {
                name: file_hash(path)
                for name, path in sorted(issue_files.items())
            }
        ),
    }


def source_integrity_digest(
    agents: dict[str, Any],
    issues: dict[str, Any],
) -> str:
    by_agent = {item["name"]: item for item in agents["agents"]}
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    records = {}
    for issue_id, contract in sorted(
        issues["source_delta_contracts"].items()
    ):
        issue = issue_by_id[issue_id]
        record = _validate_source_delta(
            issue,
            ROOT / issue["implementation"],
            by_agent[issue["agent"]],
            contract,
        )
        records[issue_id] = record
    return content_hash(
        {
            "schema_version": "1.0.0",
            "records": records,
        }
    )


def render_agent_catalog(agents: dict[str, Any]) -> str:
    lines = [
        "# Agent Catalog",
        "",
        "<!-- Generated from catalogs/AGENT_CATALOG.yaml; do not edit. -->",
        "",
        "| Agent | Owner | Type | Framework | Model | Terminal evidence | Semantic assertions | Issue count |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for agent in agents["agents"]:
        lines.append(
            f"| `{agent['name']}` | {agent['owner']} | `{agent['type']}` | "
            f"`{agent['framework']}` | "
            f"`{agent['model']}` | "
            f"`{agent['baseline_contract']['terminal_response']}` | "
            f"`{agent['baseline_contract']['semantic_assertions']}` | "
            f"{len(agent['issue_ids'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_issue_catalog(issues: dict[str, Any]) -> str:
    lines = [
        "# Issue Catalog",
        "",
        "<!-- Generated from catalogs/ISSUE_CATALOG.yaml; do not edit. -->",
        "",
        "Every issue represents one independently fixable defect and expects exactly one Insight.",
        "",
        "| Issue | Agent | Category | Severity | Expected defect |",
        "| --- | --- | --- | --- | --- |",
    ]
    for issue in issues["issues"]:
        lines.append(
            f"| <a id=\"{issue['id']}\"></a>`{issue['id']}` - {issue['title']} | "
            f"`{issue['agent']}` | "
            f"`{issue['category']}` | `{issue['severity']}` | {issue['root_cause']} |"
        )
    lines.append("")
    return "\n".join(lines)


def generate_docs(*, check: bool = False) -> None:
    agents, issues = load_catalogs()
    outputs = {
        ROOT / "AGENT_CATALOG.md": render_agent_catalog(agents),
        ROOT / "ISSUE_CATALOG.md": render_issue_catalog(issues),
    }
    stale = [
        path
        for path, content in outputs.items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]
    if check and stale:
        raise ContractError(
            "Generated catalog documents are stale: "
            + ", ".join(path.name for path in stale)
        )
    if not check:
        for path, content in outputs.items():
            path.write_text(content, encoding="utf-8", newline="\n")


def catalog_summary() -> str:
    agents, issues = load_catalogs()
    return json.dumps(
        {
            "agent_count": len(agents["agents"]),
            "issue_count": len(issues["issues"]),
            "hashes": catalog_hashes(agents, issues),
        },
        sort_keys=True,
    )
