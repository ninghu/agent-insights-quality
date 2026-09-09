"""Readable catalog views, generated without changing executable Agent assets."""

from __future__ import annotations

from html import escape

from .catalogs import Catalog


def _cell(value: object) -> str:
    return escape(str(value), quote=False).replace("|", "&#124;").replace("\n", " ")


def render_catalog_views(catalog: Catalog) -> dict[str, str]:
    agents, issues = catalog._documents
    agent_lines = [
        "# Agent Catalog", "",
        "<!-- Generated from catalogs/AGENT_CATALOG.yaml; do not edit. -->", "",
        "Each Agent owns one complete baseline and its reviewed single-root issue versions.",
        "Validation modes are reviewed data, not inferred from observed model behavior.", "",
        "| Agent | Owner | Type | Framework | Model | Terminal evidence | Semantic assertions "
        "| Validation | Issue count |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for agent in agents["agents"]:
        baseline = agent["baseline_contract"]
        values = (
            agent["name"], agent["owner"], agent["type"], agent["framework"], agent["model"],
            baseline["terminal_response"], baseline["semantic_assertions"],
            baseline["validation_mode"], len(agent["issue_ids"]),
        )
        cells = [_cell(value) if index in {1, 8} else f"`{_cell(value)}`"
                 for index, value in enumerate(values)]
        agent_lines.append("| " + " | ".join(cells) + " |")
    issue_lines = [
        "# Issue Catalog", "",
        "<!-- Generated from catalogs/ISSUE_CATALOG.yaml; do not edit. -->", "",
        "Every issue represents one independently fixable root cause and earns at most one detection.",
        f"Daily rotates {issues['selection']['issues_per_agent_daily']} issues per Agent. "
        f"Full staging covers all {len(issues['issues'])} issues and five baselines; incremental staging selects changed, "
        "missing, or incomplete targets.",
        "There is no separate deployed paired-v0 requirement. Validation modes below are reviewed "
        "catalog data; Prompt behavior remains subject to deployed staging evidence.", "",
        "| Issue | Agent | Category | Severity | Validation | Expected defect |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for issue in issues["issues"]:
        identity = _cell(issue["id"])
        cells = [
            f'<a id="{identity}"></a>`{identity}` - {_cell(issue["title"])}',
            *[f"`{_cell(issue[name])}`" for name in (
                "agent", "category", "severity", "validation_mode",
            )],
            _cell(issue["root_cause"]),
        ]
        issue_lines.append("| " + " | ".join(cells) + " |")
    return {
        "AGENT_CATALOG.md": "\n".join(agent_lines) + "\n",
        "ISSUE_CATALOG.md": "\n".join(issue_lines) + "\n",
    }


def generate_catalog_views(catalog: Catalog) -> tuple[str, ...]:
    documents = render_catalog_views(catalog)
    for name, content in documents.items():
        (catalog.root / name).write_text(content, encoding="utf-8", newline="\n")
    return tuple(documents)
