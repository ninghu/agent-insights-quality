from __future__ import annotations

from pathlib import Path

from agent_insights_quality.provisioning import deterministic_zip, _version_from_response


def test_hosted_package_is_deterministic_and_issue_specific(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('synthetic')\n", encoding="utf-8")
    first_issue = tmp_path / "first.yaml"
    first_issue.write_text("issue_id: issue-001\n", encoding="utf-8")
    second_issue = tmp_path / "second.yaml"
    second_issue.write_text("issue_id: issue-002\n", encoding="utf-8")
    first = deterministic_zip(source, extra=first_issue)
    assert first == deterministic_zip(source, extra=first_issue)
    assert first != deterministic_zip(source, extra=second_issue)


def test_agent_create_response_uses_nested_version_not_agent_id() -> None:
    assert _version_from_response(
        {
            "id": "healthcare-agent",
            "versions": {"latest": {"version": "1"}},
        }
    ) == "1"
