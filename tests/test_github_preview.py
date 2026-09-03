from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_insights_quality import github_preview
from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.github_preview import (
    AGENT_NAMES,
    PREVIEW_BRANCH,
    PREVIEW_REF,
    GitHubGitApi,
    publish_daily_email_test_preview,
    verify_daily_email_test_preview,
)
from agent_insights_quality.reporting import build_report, write_report
from agent_insights_quality.util import ContractError
from tests.test_reporting import (
    _assessments,
    _baseline_assessments,
    _manifest,
)

COMMIT_ONE = "1" * 40


class MemoryGitHubApi:
    def __init__(self) -> None:
        self.head: str | None = None
        self.commits: dict[str, str] = {}
        self.trees: dict[str, dict[str, bytes]] = {}
        self.blobs: dict[str, bytes] = {}
        self.created_trees: list[tuple[str | None, set[str]]] = []
        self.created_commits: list[tuple[str | None, str]] = []
        self.verified_paths: list[str] = []
        self._identity = 0

    def _sha(self) -> str:
        self._identity += 1
        return f"{self._identity:040x}"

    def branch_head(self) -> tuple[str, str] | None:
        if self.head is None:
            return None
        return self.head, self.commits[self.head]

    def commit_tree(self, commit_sha: str) -> str:
        return self.commits[commit_sha]

    def read_files(
        self,
        commit_sha: str,
        *,
        tree_sha: str | None = None,
    ) -> dict[str, bytes]:
        expected_tree = self.commits[commit_sha]
        assert tree_sha is None or tree_sha == expected_tree
        return dict(self.trees[expected_tree])

    def create_blob(self, content: bytes) -> str:
        sha = self._sha()
        self.blobs[sha] = content
        return sha

    def create_tree(
        self,
        entries: dict[str, str],
        *,
        base_tree: str | None,
    ) -> str:
        files = {} if base_tree is None else dict(self.trees[base_tree])
        files.update({path: self.blobs[sha] for path, sha in entries.items()})
        sha = self._sha()
        self.trees[sha] = files
        self.created_trees.append((base_tree, set(entries)))
        return sha

    def create_commit(
        self,
        tree_sha: str,
        *,
        parent: str | None,
        run_id: str,
    ) -> str:
        del run_id
        sha = COMMIT_ONE if not self.commits else self._sha()
        self.commits[sha] = tree_sha
        self.created_commits.append((parent, tree_sha))
        return sha

    def update_branch(self, commit_sha: str, *, exists: bool) -> None:
        assert exists is (self.head is not None)
        self.head = commit_sha

    def verify_file(self, path: str) -> None:
        assert self.head is not None
        assert path in self.trees[self.commits[self.head]]
        self.verified_paths.append(path)


def _report(run_id: str) -> dict:
    manifest = _manifest()
    manifest["run_id"] = run_id
    manifest["report_date"] = "2026-09-03"
    _, issues = load_catalogs()
    return build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )


def _write_preview_source(root: Path, run_id: str) -> None:
    write_report(
        _report(run_id),
        root,
        include_improvement_link=False,
    )


def test_preview_publishes_orphan_allowlist_and_permanent_links(
    tmp_path: Path,
) -> None:
    run_id = "aiq-20260903-r01"
    source = tmp_path / "final-report"
    _write_preview_source(source, run_id)
    api = MemoryGitHubApi()

    publication = publish_daily_email_test_preview(
        source,
        run_id=run_id,
        now=datetime(2026, 9, 3, 14, 30, tzinfo=UTC),
        api=api,
    )

    assert publication["branch"] == PREVIEW_BRANCH
    assert publication["ref"] == PREVIEW_REF
    assert publication["commit_sha"] == COMMIT_ONE
    assert "expires_at" not in publication
    assert publication["report_url"].endswith(f"/{run_id}/report.md")
    assert set(publication["agent_urls"]) == set(AGENT_NAMES)
    assert api.created_trees[0][0] is None
    assert api.created_commits[0][0] is None
    files = api.trees[api.commits[api.head]]
    assert set(files) == {
        f"{run_id}/.aiq-preview.json",
        f"{run_id}/report.json",
        f"{run_id}/report.md",
        *(f"{run_id}/agents/{agent_name}.md" for agent_name in AGENT_NAMES),
    }
    manifest = read_json_bytes(files[f"{run_id}/.aiq-preview.json"])
    assert manifest["created_at"] == "2026-09-03T14:30:00+00:00"
    assert "expires_at" not in manifest
    assert api.verified_paths == [
        f"{run_id}/report.md",
        *(f"{run_id}/agents/{agent_name}.md" for agent_name in AGENT_NAMES),
    ]


def test_preview_reuses_identical_run_without_mutating_branch(
    tmp_path: Path,
) -> None:
    run_id = "aiq-20260903-r02"
    source = tmp_path / "final-report"
    _write_preview_source(source, run_id)
    api = MemoryGitHubApi()
    first = publish_daily_email_test_preview(source, run_id=run_id, api=api)
    commit_count = len(api.created_commits)

    second = publish_daily_email_test_preview(source, run_id=run_id, api=api)

    assert second == first
    assert len(api.created_commits) == commit_count
    verify_daily_email_test_preview(source, first, api=api)


def test_preview_rejects_divergent_or_unmanaged_existing_content(
    tmp_path: Path,
) -> None:
    run_id = "aiq-20260903-r03"
    source = tmp_path / "final-report"
    _write_preview_source(source, run_id)
    api = MemoryGitHubApi()
    publish_daily_email_test_preview(source, run_id=run_id, api=api)
    tree = api.trees[api.commits[api.head]]
    tree[f"{run_id}/report.md"] += b"\ndivergent\n"

    with pytest.raises(ContractError, match="content binding|divergent"):
        publish_daily_email_test_preview(source, run_id=run_id, api=api)

    _write_preview_source(source, run_id)
    tree[f"{run_id}/report.md"] = (source / "report.md").read_bytes()
    tree["README.md"] = b"unmanaged\n"
    with pytest.raises(ContractError, match="unmanaged path"):
        publish_daily_email_test_preview(source, run_id=run_id, api=api)


def test_preview_appends_without_modifying_existing_run(tmp_path: Path) -> None:
    first_run = "aiq-20260903-r04"
    second_run = "aiq-20260903-r05"
    api = MemoryGitHubApi()
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    _write_preview_source(first_source, first_run)
    _write_preview_source(second_source, second_run)
    publish_daily_email_test_preview(first_source, run_id=first_run, api=api)
    first_files = {
        path: content
        for path, content in api.trees[api.commits[api.head]].items()
        if path.startswith(f"{first_run}/")
    }

    publish_daily_email_test_preview(second_source, run_id=second_run, api=api)

    final_files = api.trees[api.commits[api.head]]
    assert {
        path: content
        for path, content in final_files.items()
        if path.startswith(f"{first_run}/")
    } == first_files
    assert any(path.startswith(f"{second_run}/") for path in final_files)
    assert api.created_trees[-1][0] is not None
    assert api.created_commits[-1][0] == COMMIT_ONE


def test_preview_append_does_not_rerender_historical_runs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first_run = "aiq-20260903-r08"
    second_run = "aiq-20260903-r09"
    api = MemoryGitHubApi()
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    _write_preview_source(first_source, first_run)
    _write_preview_source(second_source, second_run)
    publish_daily_email_test_preview(first_source, run_id=first_run, api=api)
    original_validate = github_preview.validate_report
    original_report = github_preview.render_markdown
    original_agent = github_preview.render_agent_markdown

    def validate(report: dict) -> None:
        assert report["run_id"] == second_run
        original_validate(report)

    def report_markdown(report: dict, **kwargs) -> str:
        assert report["run_id"] == second_run
        return original_report(report, **kwargs)

    def agent_markdown(report: dict, agent_name: str) -> str:
        assert report["run_id"] == second_run
        return original_agent(report, agent_name)

    monkeypatch.setattr(github_preview, "validate_report", validate)
    monkeypatch.setattr(github_preview, "render_markdown", report_markdown)
    monkeypatch.setattr(github_preview, "render_agent_markdown", agent_markdown)

    publish_daily_email_test_preview(second_source, run_id=second_run, api=api)


@pytest.mark.parametrize(
    "private_value, error",
    [
        ("See [private](//dev.azure.com/private/project/query).", "private link"),
        ('See <a href="mailto:synthetic@microsoft.com">private</a>.', "private link"),
        ("Session ID: private-session-123.", "private data"),
    ],
)
def test_preview_rejects_private_content_before_any_git_write(
    tmp_path: Path,
    private_value: str,
    error: str,
) -> None:
    run_id = "aiq-20260903-r06"
    source = tmp_path / "final-report"
    report = _report(run_id)
    report["baseline"][0]["assessment"]["ownership_reason"] = private_value
    write_report(report, source, include_improvement_link=False)
    api = MemoryGitHubApi()

    with pytest.raises(ContractError, match=error):
        publish_daily_email_test_preview(source, run_id=run_id, api=api)

    assert api.created_trees == []
    assert api.created_commits == []


def test_archive_validation_rejects_export_ignored_git_content(
    monkeypatch,
) -> None:
    commit_sha = "a" * 40
    tree_sha = "b" * 40
    run_id = "aiq-20260903-r07"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(
            f"ninghu-agent-insights-quality-{commit_sha[:7]}/{run_id}/report.md",
            "synthetic\n",
        )
    endpoints = []
    api = GitHubGitApi()

    def request(method: str, endpoint: str, body=None):
        del body
        endpoints.append((method, endpoint))
        return {
            "truncated": False,
            "tree": [
                {"path": run_id, "mode": "040000", "type": "tree"},
                {
                    "path": f"{run_id}/report.md",
                    "mode": "100644",
                    "type": "blob",
                },
                {
                    "path": ".gitattributes",
                    "mode": "100644",
                    "type": "blob",
                },
            ],
        }

    monkeypatch.setattr(api, "_request", request)
    monkeypatch.setattr(api, "_request_bytes", lambda _endpoint: archive.getvalue())

    with pytest.raises(ContractError, match="complete Git tree"):
        api.read_files(commit_sha, tree_sha=tree_sha)

    assert endpoints == [
        (
            "GET",
            "repos/ninghu/agent-insights-quality/git/trees/"
            f"{tree_sha}?recursive=1",
        )
    ]


def read_json_bytes(content: bytes) -> dict:
    import json

    value = json.loads(content)
    assert isinstance(value, dict)
    return value
