from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

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

EXPECTED_ASSIGNMENTS = {
    "weather-agent": 6,
    "healthcare-agent": 6,
    "finance-agent": 8,
    "travel-agent": 8,
    "support-ticket-agent": 8,
}


def _validate_schema(value: dict[str, Any], schema_path: Path, label: str) -> None:
    schema = read_json(schema_path)
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
    agent_items = agents["agents"]
    issue_items = issues["issues"]
    by_agent = {item["name"]: item for item in agent_items}
    if set(by_agent) != set(EXPECTED_ASSIGNMENTS):
        raise ContractError("Agent Catalog must contain the five fixed Agents")

    ids = [item["id"] for item in issue_items]
    expected_ids = [f"issue-{number:03d}" for number in range(1, 37)]
    if ids != expected_ids:
        raise ContractError("Issue IDs must be ordered and continuous from issue-001 to issue-036")
    if len(ids) != len(set(ids)):
        raise ContractError("Issue IDs must be unique")

    counts = Counter(item["agent"] for item in issue_items)
    if dict(counts) != EXPECTED_ASSIGNMENTS:
        raise ContractError("Issue assignments do not match the reviewed fixed distribution")

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
            _validate_implementation(issue, path)


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


def _validate_baseline(agent: dict[str, Any]) -> None:
    root = ROOT / agent["baseline_path"]
    metadata = read_yaml(root / "implementation.yaml")
    if metadata.get("issue_id") != "v0" or metadata.get("agent_name") != agent["name"]:
        raise ContractError(f"{agent['name']} baseline metadata is invalid")
    traffic = json.loads((root / "traffic.json").read_text(encoding="utf-8"))
    requests = traffic.get("requests") if isinstance(traffic, dict) else None
    if not isinstance(requests, list) or len(requests) < 5:
        raise ContractError(f"{agent['name']} baseline requires at least five requests")
    if agent["type"] == "prompt":
        definition = json.loads((root / "definition.json").read_text(encoding="utf-8"))
        if definition.get("definition", {}).get("model") != "gpt-5.6-terra":
            raise ContractError(f"{agent['name']} Prompt definition must use GPT-5.6 Terra")


def _validate_implementation(issue: dict[str, Any], root: Path) -> None:
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
    traffic = json.loads((root / "traffic.json").read_text(encoding="utf-8"))
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


def render_agent_catalog(agents: dict[str, Any]) -> str:
    lines = [
        "# Agent Catalog",
        "",
        "<!-- Generated from catalogs/AGENT_CATALOG.yaml; do not edit. -->",
        "",
        "| Agent | Type | Framework | Model | Issue count |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for agent in agents["agents"]:
        lines.append(
            f"| `{agent['name']}` | `{agent['type']}` | `{agent['framework']}` | "
            f"`{agent['model']}` | {len(agent['issue_ids'])} |"
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
