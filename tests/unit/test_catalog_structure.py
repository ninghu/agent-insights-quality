"""Offline source structure checks, not deployed behavior or runner qualification."""

import ast
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
AGENTS = yaml.safe_load(
    (ROOT / "catalogs" / "AGENT_CATALOG.yaml").read_text(encoding="utf-8")
)["agents"]
ISSUES = yaml.safe_load(
    (ROOT / "catalogs" / "ISSUE_CATALOG.yaml").read_text(encoding="utf-8")
)["issues"]
OWNERSHIP = {
    "weather-agent": (*range(1, 7), 38),
    "healthcare-agent": (*range(7, 13), 37),
    "finance-agent": (*range(13, 21), 40),
    "travel-agent": range(21, 29),
    "support-ticket-agent": (*range(29, 37), 39),
}
VERSIONS = [
    pytest.param(
        agent["name"], version, agent["type"], id=f"{agent['name']}-{version}"
    )
    for agent in AGENTS
    for version in ["v0", *agent["issue_ids"]]
]


def test_catalog_ownership():
    assert len(AGENTS) == 5
    assert {agent["name"] for agent in AGENTS} == set(OWNERSHIP)
    assert len(ISSUES) == 40
    by_id = {issue["id"]: issue for issue in ISSUES}
    assert set(by_id) == {f"issue-{number:03d}" for number in range(1, 41)}

    for agent in AGENTS:
        name = agent["name"]
        expected_ids = {f"issue-{number:03d}" for number in OWNERSHIP[name]}
        assert len(agent["issue_ids"]) == len(expected_ids)
        assert set(agent["issue_ids"]) == expected_ids
        assert Path(agent["baseline_path"]) == Path("agents", name, "v0")
        for issue_id in expected_ids:
            issue = by_id[issue_id]
            assert issue["agent"] == name
            assert Path(issue["implementation"]) == Path(
                "agents", name, "issues", issue_id
            )
    assert sum(issue["category"] == "safety_guardrails" for issue in ISSUES) == 9
    assert all(by_id[f"issue-{number:03d}"]["category"] == "safety_guardrails" for number in range(37, 41))


@pytest.mark.parametrize("agent_name,version,agent_type", VERSIONS)
def test_version_local_sources(agent_name, version, agent_type):
    agent_root = ROOT / "agents" / agent_name
    directory = agent_root / "v0" if version == "v0" else agent_root / "issues" / version
    assert directory.is_dir()
    manifest = yaml.safe_load(
        (directory / "implementation.yaml").read_text(encoding="utf-8")
    )
    assert manifest["agent_name"] == agent_name
    assert manifest["issue_id"] == version
    traffic = json.loads((directory / "traffic.json").read_text(encoding="utf-8"))
    assert traffic["agent_name"] == agent_name
    assert traffic["logical_version"] == version
    assert traffic["requests"]

    if agent_type == "prompt":
        document = json.loads(
            (directory / "definition.json").read_text(encoding="utf-8")
        )
        definition = document["definition"]
        assert definition["kind"] == "prompt"
        assert definition["instructions"].strip()
        assert not document.get("tools")
        assert not definition.get("tools")
    else:
        assert agent_type in {"hosted_code", "hosted_custom_container"}
        source = directory / "source"
        assert (source / "__init__.py").is_file()
        assert (source / "app.py").is_file()
        for path in sorted(source.rglob("*.py")):
            assert path.resolve().is_relative_to(source.resolve()), path
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            compile(tree, str(path), "exec")
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    package = path.parent
                    for _ in range(node.level - 1):
                        package = package.parent
                    assert package.is_relative_to(source), path
                    if node.module:
                        module = package.joinpath(*node.module.split("."))
                        assert (
                            module.with_suffix(".py").is_file()
                            or (module / "__init__.py").is_file()
                        ), (path, node.module)

    for path in sorted(directory.rglob("*")):
        if path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".yaml":
            assert isinstance(
                yaml.safe_load(path.read_text(encoding="utf-8")), dict
            ), path
        elif path.suffix == ".py" and path.parent == directory:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
