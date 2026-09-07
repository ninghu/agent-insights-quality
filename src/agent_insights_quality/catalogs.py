"""Cheap catalog loading; source and schema checks are an explicit separate operation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .contracts import Target
from .results import UnitId


@dataclass(frozen=True)
class Catalog:
    root: Path
    agents: tuple[str, ...]
    targets: tuple[Target, ...]
    _documents: tuple[dict[str, Any], dict[str, Any]] = field(repr=False)

    def target(self, key: str) -> Target:
        for target in self.targets:
            if target.key == key:
                return target
        raise KeyError(key)

    def for_agent(self, name: str) -> tuple[Target, ...]:
        if name not in self.agents:
            raise KeyError(name)
        return tuple(target for target in self.targets if target.unit_id.agent == name)


def _owned_path(root: Path, supplied: str, expected: Path) -> Path:
    if not isinstance(supplied, str) or Path(supplied) != expected:
        raise ValueError("Catalog paths must match version ownership")
    path = (root / expected).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Catalog path escapes the repository")
    return path


def load_catalog(root: Path) -> Catalog:
    """Read each YAML authority once, without walking or hashing Agent sources."""
    root = Path(root).resolve()
    documents = tuple(
        yaml.safe_load((root / "catalogs" / filename).read_text(encoding="utf-8"))
        for filename in ("AGENT_CATALOG.yaml", "ISSUE_CATALOG.yaml")
    )
    agents_doc, issues_doc = documents
    if not isinstance(agents_doc, dict) or not isinstance(issues_doc, dict):
        raise ValueError("Catalog documents must be objects")
    agents, issues = agents_doc.get("agents"), issues_doc.get("issues")
    if not isinstance(agents, list) or not isinstance(issues, list):
        raise ValueError("Catalog inventories must be lists")
    by_id = {}
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("id"), str):
            raise ValueError("Invalid issue catalog entry")
        if issue["id"] in by_id:
            raise ValueError("Duplicate issue identity")
        by_id[issue["id"]] = issue

    names, targets, claimed = [], [], set()
    for agent in agents:
        if not isinstance(agent, dict):
            raise ValueError("Invalid Agent catalog entry")
        name = agent["name"]
        if name in names:
            raise ValueError("Duplicate Agent identity")
        names.append(name)
        baseline = _owned_path(
            root, agent["baseline_path"], Path("agents", name, "v0")
        )
        agent_type = agent["type"]
        if agent_type not in {"prompt", "hosted_code", "hosted_custom_container"}:
            raise ValueError("Unknown Agent type")
        baseline_contract = agent["baseline_contract"]
        if baseline_contract.get("validation_mode") != "baseline":
            raise ValueError("Baseline validation mode must be baseline")
        targets.append(Target(
            UnitId(name, "v0"), agent_type, "baseline",
            baseline, baseline, baseline_contract,
        ))
        for issue_id in agent["issue_ids"]:
            if issue_id in claimed or issue_id not in by_id:
                raise ValueError("Duplicate or missing issue ownership")
            issue = by_id[issue_id]
            if issue["agent"] != name:
                raise ValueError("Issue owner disagrees with Agent catalog")
            claimed.add(issue_id)
            version = _owned_path(
                root, issue["implementation"], Path("agents", name, "issues", issue_id)
            )
            mode = issue["validation_mode"]
            if mode not in {"deterministic", "model_mediated"}:
                raise ValueError("Unknown reviewed validation mode")
            targets.append(Target(
                UnitId(name, issue_id), agent_type, mode, version, baseline, issue,
            ))
    if claimed != set(by_id):
        raise ValueError("Unowned issue catalog entry")
    roots = [target.version_root for target in targets]
    if len(set(roots)) != len(roots):
        raise ValueError("Version paths must be unique")
    return Catalog(root, tuple(names), tuple(targets), (agents_doc, issues_doc))


def validate_catalog(catalog: Catalog) -> None:
    """Validate reviewed inventory and complete version-local assets, offline."""
    from .selection import deployment_inputs
    from .traffic import load_attempts

    for name, document in zip(("agent", "issue"), catalog._documents, strict=True):
        schema = json.loads(
            (catalog.root / "schemas" / f"{name}-catalog.schema.json")
            .read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
    if len(catalog.agents) != 5 or len(catalog.targets) != 41:
        raise ValueError("Expected five Agents and 36 issues")
    issue_ids = [
        target.unit_id.logical_version for target in catalog.targets if not target.is_baseline
    ]
    if len(issue_ids) != 36 or set(issue_ids) != {
        f"issue-{number:03d}" for number in range(1, 37)
    }:
        raise ValueError("Reviewed issue inventory changed")
    # Ownership comes from the two agreeing catalogs, not numeric issue ranges.
    if any(sum(not target.is_baseline for target in catalog.for_agent(name)) < 4
           for name in catalog.agents):
        raise ValueError("Daily lanes require at least four distinct issues")
    selection = catalog._documents[1]["selection"]
    if selection != {
        "issues_per_agent_daily": 4,
        "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    }:
        raise ValueError("Reviewed Daily selection contract changed")

    prompt_schema = json.loads(
        (catalog.root / "schemas" / "prompt-definition.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(prompt_schema)
    prompt_validator = Draft202012Validator(prompt_schema)
    for target in catalog.targets:
        local_assets = ["implementation.yaml", "traffic.json"]
        local_assets += ["definition.json"] if target.is_prompt else ["source"]
        for name in local_assets:
            path = target.version_root / name
            if not path.resolve().is_relative_to(target.version_root):
                raise ValueError("Agent assets must be version-local")
        for path in deployment_inputs(target):
            if not path.exists():
                raise ValueError(f"Missing deployment input for {target.key}")
            if not path.resolve().is_relative_to(catalog.root):
                raise ValueError("Deployment input escapes the repository")
        manifest = yaml.safe_load(
            (target.version_root / "implementation.yaml").read_text(encoding="utf-8")
        )
        if (
            manifest["agent_name"] != target.unit_id.agent
            or manifest["issue_id"] != target.unit_id.logical_version
        ):
            raise ValueError("Implementation identity disagrees with catalog")
        if target.is_prompt:
            document = json.loads(
                (target.version_root / "definition.json").read_text(encoding="utf-8")
            )
            prompt_validator.validate(document)
            if (
                document["name"] != target.unit_id.agent
                or document["metadata"]["logical_version"] != target.unit_id.logical_version
            ):
                raise ValueError("Prompt definition identity disagrees with catalog")
        else:
            source = target.version_root / "source"
            if not (source / "__init__.py").is_file() or not (source / "app.py").is_file():
                raise ValueError("Hosted version needs complete local source")
            for path in source.rglob("*"):
                if not path.resolve().is_relative_to(source.resolve()):
                    raise ValueError("Hosted source escapes its version")
        load_attempts(target)
