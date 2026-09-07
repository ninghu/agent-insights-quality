"""Offline loading is cheap; full asset validation is deliberate."""

from pathlib import Path

import pytest
import yaml

from agent_insights_quality.catalogs import load_catalog, validate_catalog
from agent_insights_quality.contracts import Target


ROOT = Path(__file__).resolve().parents[2]


def test_loading_reads_each_catalog_once_and_never_walks_sources(monkeypatch):
    original = yaml.safe_load
    calls = []

    def read(value):
        calls.append(value)
        return original(value)

    def forbidden_walk(*args, **kwargs):
        pytest.fail("Cheap loading must not walk Agent source")

    monkeypatch.setattr(yaml, "safe_load", read)
    monkeypatch.setattr(Path, "rglob", forbidden_walk)
    catalog = load_catalog(ROOT)
    assert len(catalog.agents) == 5
    assert len(catalog.targets) == 41
    assert len({target.key for target in catalog.targets}) == 41
    assert len({target.version_root for target in catalog.targets}) == 41
    for target in catalog.targets:
        assert isinstance(target, Target)
        assert catalog.target(target.key) is target
        assert target in catalog.for_agent(target.unit_id.agent)
        assert target.runtime_name("daily") == target.unit_id.agent
        assert target.runtime_name("staging") == target.key.replace("/", "-")
        assert target.expectation["validation_mode"] == target.validation_mode
    assert sum(target.is_baseline for target in catalog.targets) == 5
    assert len(calls) == 2
    with pytest.raises(KeyError):
        catalog.target("not-a-target")
    with pytest.raises(KeyError):
        catalog.for_agent("not-an-agent")


@pytest.fixture
def documents(tmp_path):
    directory = tmp_path / "catalogs"
    directory.mkdir()
    docs = [
        yaml.safe_load((ROOT / "catalogs" / name).read_text(encoding="utf-8"))
        for name in ("AGENT_CATALOG.yaml", "ISSUE_CATALOG.yaml")
    ]

    def save():
        for name, document in zip(
            ("AGENT_CATALOG.yaml", "ISSUE_CATALOG.yaml"), docs, strict=True,
        ):
            (directory / name).write_text(yaml.safe_dump(document), encoding="utf-8")
        return tmp_path

    return docs, save


def test_loading_does_not_require_deployment_sources(documents):
    _, save = documents
    catalog = load_catalog(save())
    assert len(catalog.targets) == 41
    assert not catalog.targets[0].version_root.exists()


@pytest.mark.parametrize("violation", [
    "duplicate_agent", "duplicate_issue", "double_claim", "wrong_owner",
    "missing_issue", "unclaimed_issue", "escape", "wrong_path", "unknown_mode",
])
def test_loader_rejects_ambiguous_ownership(documents, violation):
    (agents, issues), save = documents
    if violation == "duplicate_agent":
        agents["agents"].append(agents["agents"][0])
    elif violation == "duplicate_issue":
        issues["issues"].append(issues["issues"][0])
    elif violation == "double_claim":
        agents["agents"][0]["issue_ids"].append("issue-001")
    elif violation == "wrong_owner":
        issues["issues"][0]["agent"] = "healthcare-agent"
    elif violation == "missing_issue":
        issues["issues"].pop(0)
    elif violation == "unclaimed_issue":
        agents["agents"][0]["issue_ids"].pop(0)
    elif violation == "escape":
        agents["agents"][0]["baseline_path"] = "../outside"
    elif violation == "wrong_path":
        issues["issues"][0]["implementation"] = "agents/weather-agent/issues/issue-002"
    else:
        issues["issues"][0]["validation_mode"] = "infer_from_response"
    with pytest.raises(ValueError):
        load_catalog(save())


def test_explicit_validation_checks_all_reviewed_assets():
    validate_catalog(load_catalog(ROOT))
