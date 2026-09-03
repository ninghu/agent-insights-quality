from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import re
import stat
import subprocess
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.reporting import (
    render_agent_markdown,
    render_markdown,
    validate_report,
)
from agent_insights_quality.util import ROOT, ContractError, content_hash, read_json

REPOSITORY = "ninghu/agent-insights-quality"
PREVIEW_BRANCH = "aiq-email-test-preview"
PREVIEW_REF = f"refs/heads/{PREVIEW_BRANCH}"
AGENT_NAMES = (
    "weather-agent",
    "healthcare-agent",
    "finance-agent",
    "travel-agent",
    "support-ticket-agent",
)
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_RUN_PATTERN = re.compile(r"^aiq-[0-9]{8}-r([0-9]{2,})$", re.ASCII)
_URL_PATTERN = re.compile(
    r"(?:(?:https?:)?//)[^\s<>)\]\"']+",
    re.IGNORECASE | re.ASCII,
)
_MARKDOWN_LINK_PATTERN = re.compile(
    r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)",
    re.ASCII,
)
_HTML_LINK_PATTERN = re.compile(
    r"\b(?:href|src)\s*=\s*\\?[\"']([^\\\"']+)",
    re.IGNORECASE | re.ASCII,
)
_PRIVATE_IDENTIFIER_PATTERN = re.compile(
    r"(?i)\b(?:provider|session|response)[ _-]?(?:id|reference)"
    r"\s*[:=]\s*[A-Za-z0-9][A-Za-z0-9._:-]{5,}",
    re.ASCII,
)
_FORBIDDEN_PUBLIC_TEXT = (
    re.compile(r"(?i)\bconnectionstring\s*="),
    re.compile(r"(?i)[\"']?(?:client_secret|access_token|refresh_token)[\"']?\s*[:=]"),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\b"),
    re.compile(r"(?i)/subscriptions/[0-9a-f-]{36}"),
    re.compile(r"(?i)https://[a-z0-9.-]+\.services\.ai\.azure\.com"),
    re.compile(r"(?i)https://(?:dev\.azure\.com|[a-z0-9.-]+\.visualstudio\.com)/"),
)
_PUBLIC_REPOSITORY_URL = f"https://github.com/{REPOSITORY}/"
_MANIFEST_SCHEMA = ROOT / "schemas" / "daily-email-test-preview.schema.json"


class GitHubGitApi:
    def _request(
        self,
        method: str,
        endpoint: str,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        if method not in {"GET", "POST", "PATCH"} or not endpoint.isascii():
            raise ContractError("GitHub preview API request is invalid")
        command = ["gh", "api", "--method", method, endpoint]
        rendered = None
        if body is not None:
            command.extend(["--input", "-"])
            rendered = json.dumps(body, sort_keys=True, ensure_ascii=True)
        process = subprocess.run(
            command,
            cwd=ROOT,
            input=rendered,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            raise ContractError("GitHub preview API request failed")
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise ContractError("GitHub preview API returned invalid JSON") from error

    def _request_bytes(self, endpoint: str) -> bytes:
        if not endpoint.isascii():
            raise ContractError("GitHub preview API request is invalid")
        process = subprocess.run(
            ["gh", "api", "--method", "GET", endpoint],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            raise ContractError("GitHub preview API request failed")
        return process.stdout

    def branch_head(self) -> tuple[str, str] | None:
        values = self._request(
            "GET",
            f"repos/{REPOSITORY}/git/matching-refs/heads/{PREVIEW_BRANCH}",
        )
        if not isinstance(values, list):
            raise ContractError("GitHub preview ref response is invalid")
        exact = [item for item in values if item.get("ref") == PREVIEW_REF]
        if len(exact) > 1:
            raise ContractError("GitHub preview ref is ambiguous")
        if not exact:
            return None
        commit_sha = _validated_sha(exact[0].get("object", {}).get("sha"))
        return commit_sha, self.commit_tree(commit_sha)

    def commit_tree(self, commit_sha: str) -> str:
        commit_sha = _validated_sha(commit_sha)
        commit = self._request("GET", f"repos/{REPOSITORY}/git/commits/{commit_sha}")
        return _validated_sha(commit.get("tree", {}).get("sha"))

    def read_files(
        self,
        commit_sha: str,
        *,
        tree_sha: str | None = None,
    ) -> dict[str, bytes]:
        commit_sha = _validated_sha(commit_sha)
        resolved_tree_sha = (
            self.commit_tree(commit_sha)
            if tree_sha is None
            else _validated_sha(tree_sha)
        )
        tree = self._request(
            "GET",
            f"repos/{REPOSITORY}/git/trees/{resolved_tree_sha}?recursive=1",
        )
        if tree.get("truncated") is not False or not isinstance(
            tree.get("tree"), list
        ):
            raise ContractError("GitHub preview tree is incomplete")
        tree_files: set[str] = set()
        tree_directories: set[str] = set()
        for item in tree["tree"]:
            path = _validated_tree_path(item.get("path"))
            if item.get("type") == "tree" and item.get("mode") == "040000":
                tree_directories.add(path)
            elif item.get("type") == "blob" and item.get("mode") == "100644":
                tree_files.add(path)
            else:
                raise ContractError("GitHub preview tree contains an invalid entry")
        archive = self._request_bytes(
            f"repos/{REPOSITORY}/zipball/{commit_sha}",
        )
        result: dict[str, bytes] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as stream:
                roots: set[str] = set()
                for entry in stream.infolist():
                    parts = PurePosixPath(entry.filename).parts
                    if not parts:
                        raise ContractError("GitHub preview archive path is invalid")
                    roots.add(parts[0])
                    if entry.is_dir():
                        continue
                    mode = entry.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ContractError("GitHub preview archive contains a symlink")
                    if len(parts) < 2:
                        raise ContractError("GitHub preview archive path is invalid")
                    path = _validated_tree_path("/".join(parts[1:]))
                    if path in result:
                        raise ContractError("GitHub preview archive path is duplicated")
                    result[path] = stream.read(entry)
        except zipfile.BadZipFile as error:
            raise ContractError("GitHub preview API returned an invalid archive") from error
        if len(roots) != 1:
            raise ContractError("GitHub preview archive root is ambiguous")
        expected_directories = {
            parent.as_posix()
            for path in result
            for parent in PurePosixPath(path).parents
            if parent != PurePosixPath(".")
        }
        if set(result) != tree_files or tree_directories != expected_directories:
            raise ContractError(
                "GitHub preview archive does not match the complete Git tree"
            )
        return result

    def create_blob(self, content: bytes) -> str:
        value = self._request(
            "POST",
            f"repos/{REPOSITORY}/git/blobs",
            {
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
        )
        return _validated_sha(value.get("sha"))

    def create_tree(
        self,
        entries: Mapping[str, str],
        *,
        base_tree: str | None,
    ) -> str:
        tree = [
            {
                "path": _validated_tree_path(path),
                "mode": "100644",
                "type": "blob",
                "sha": _validated_sha(sha),
            }
            for path, sha in sorted(entries.items())
        ]
        body: dict[str, Any] = {"tree": tree}
        if base_tree is not None:
            body["base_tree"] = _validated_sha(base_tree)
        value = self._request("POST", f"repos/{REPOSITORY}/git/trees", body)
        return _validated_sha(value.get("sha"))

    def create_commit(
        self,
        tree_sha: str,
        *,
        parent: str | None,
        run_id: str,
    ) -> str:
        _validate_run_id(run_id)
        body = {
            "message": f"Publish Daily email test preview {run_id}",
            "tree": _validated_sha(tree_sha),
            "parents": [] if parent is None else [_validated_sha(parent)],
        }
        value = self._request("POST", f"repos/{REPOSITORY}/git/commits", body)
        return _validated_sha(value.get("sha"))

    def update_branch(self, commit_sha: str, *, exists: bool) -> None:
        commit_sha = _validated_sha(commit_sha)
        if exists:
            self._request(
                "PATCH",
                f"repos/{REPOSITORY}/git/refs/heads/{PREVIEW_BRANCH}",
                {"sha": commit_sha, "force": False},
            )
        else:
            self._request(
                "POST",
                f"repos/{REPOSITORY}/git/refs",
                {"ref": PREVIEW_REF, "sha": commit_sha},
            )

    def verify_file(self, path: str) -> None:
        path = _validated_tree_path(path)
        value = self._request(
            "GET",
            f"repos/{REPOSITORY}/contents/{path}?ref={PREVIEW_BRANCH}",
        )
        if value.get("type") != "file" or not _SHA_PATTERN.fullmatch(
            str(value.get("sha") or "")
        ):
            raise ContractError("GitHub preview link target is unavailable")


def preview_links(run_id: str) -> dict[str, Any]:
    _validate_run_id(run_id)
    base = f"{_PUBLIC_REPOSITORY_URL}blob/{PREVIEW_BRANCH}/{run_id}/"
    return {
        "repository": REPOSITORY,
        "branch": PREVIEW_BRANCH,
        "ref": PREVIEW_REF,
        "run_id": run_id,
        "directory": run_id,
        "report_url": base + "report.md",
        "agent_urls": {
            agent_name: base + f"agents/{agent_name}.md"
            for agent_name in AGENT_NAMES
        },
    }


def publish_daily_email_test_preview(
    report_root: Path,
    *,
    run_id: str,
    now: datetime | None = None,
    api: GitHubGitApi | None = None,
) -> dict[str, Any]:
    _validate_run_id(run_id)
    artifacts = _generated_artifacts(report_root, run_id)
    git = api or GitHubGitApi()
    head = git.branch_head()
    existing_files = (
        {} if head is None else git.read_files(head[0], tree_sha=head[1])
    )
    existing_manifests = _validate_branch(existing_files)
    if run_id in existing_manifests:
        prefix = f"{run_id}/"
        existing = {
            path.removeprefix(prefix): content
            for path, content in existing_files.items()
            if path.startswith(prefix) and path != f"{run_id}/.aiq-preview.json"
        }
        if existing != artifacts:
            raise ContractError("Existing GitHub preview run content is divergent")
        manifest = existing_manifests[run_id]
        publication = _publication(
            manifest,
            commit_sha=head[0],
        )
        _verify_links(git, publication)
        return publication

    source_time = now or datetime.now(UTC)
    if source_time.utcoffset() is None:
        raise ContractError("GitHub preview creation time must include a timezone")
    created_at = source_time.astimezone(UTC)
    manifest = _build_manifest(run_id, artifacts, created_at)
    run_files = {**artifacts, ".aiq-preview.json": _json_bytes(manifest)}
    blob_shas = {
        f"{run_id}/{path}": git.create_blob(content)
        for path, content in sorted(run_files.items())
    }
    tree_sha = git.create_tree(
        blob_shas,
        base_tree=head[1] if head is not None else None,
    )
    commit_sha = git.create_commit(
        tree_sha,
        parent=head[0] if head is not None else None,
        run_id=run_id,
    )
    git.update_branch(commit_sha, exists=head is not None)
    verified_head = git.branch_head()
    if verified_head is None or verified_head[0] != commit_sha:
        raise ContractError("GitHub preview ref did not reach the created commit")
    verified_files = git.read_files(
        verified_head[0],
        tree_sha=verified_head[1],
    )
    verified_manifests = _validate_branch(verified_files)
    if run_id not in verified_manifests:
        raise ContractError("GitHub preview run is missing after publication")
    publication = _publication(
        verified_manifests[run_id],
        commit_sha=commit_sha,
    )
    _verify_links(git, publication)
    return publication


def verify_daily_email_test_preview(
    report_root: Path,
    publication: Mapping[str, Any],
    *,
    api: GitHubGitApi | None = None,
) -> None:
    run_id = str(publication.get("run_id") or "")
    validate_preview_publication(publication, run_id=run_id)
    expected = _generated_artifacts(report_root, run_id)
    git = api or GitHubGitApi()
    committed_files = git.read_files(str(publication["commit_sha"]))
    committed_manifests = _validate_branch(committed_files)
    _assert_published_run(
        committed_files,
        committed_manifests,
        expected,
        publication,
    )
    head = git.branch_head()
    if head is None:
        raise ContractError("GitHub preview branch is missing")
    current_files = git.read_files(head[0], tree_sha=head[1])
    current_manifests = _validate_branch(current_files)
    _assert_published_run(
        current_files,
        current_manifests,
        expected,
        publication,
    )
    _verify_links(git, publication)


def validate_preview_publication(
    value: Mapping[str, Any],
    *,
    run_id: str,
) -> None:
    expected_links = preview_links(run_id)
    expected_keys = {
        "schema_version",
        "kind",
        "repository",
        "branch",
        "ref",
        "run_id",
        "directory",
        "created_at",
        "commit_sha",
        "content_digest",
        "manifest_digest",
        "report_url",
        "agent_urls",
    }
    if (
        set(value) != expected_keys
        or value.get("schema_version") != "1.0.0"
        or value.get("kind") != "daily-email-test-preview-publication"
        or any(value.get(key) != expected_links[key] for key in expected_links)
        or _SHA_PATTERN.fullmatch(str(value.get("commit_sha") or "")) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("content_digest") or ""))
        is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("manifest_digest") or ""))
        is None
    ):
        raise ContractError("GitHub preview publication binding is invalid")
    try:
        moment = datetime.fromisoformat(str(value["created_at"]))
    except ValueError as error:
        raise ContractError("GitHub preview publication time is invalid") from error
    if moment.utcoffset() is None:
        raise ContractError("GitHub preview publication time must include a timezone")


def bind_preview_publication(
    request: Mapping[str, Any],
    publication: Mapping[str, Any],
) -> dict[str, Any]:
    run_id = str(publication.get("run_id") or "")
    validate_preview_publication(publication, run_id=run_id)
    links = preview_links(run_id)
    rendered = str(request.get("html") or "")
    if links["report_url"] not in rendered or any(
        url not in rendered for url in links["agent_urls"].values()
    ):
        raise ContractError("Email request does not contain every GitHub preview link")
    result = dict(request)
    if "preview" in result:
        raise ContractError("Email request already contains a preview binding")
    result["preview"] = dict(publication)
    return result


def _generated_artifacts(report_root: Path, run_id: str) -> dict[str, bytes]:
    report = read_json(report_root / "report.json")
    validate_report(report)
    if report.get("profile") != "daily" or report.get("run_id") != run_id:
        raise ContractError("GitHub preview requires the exact Daily test report")
    if {item.get("agent") for item in report["baseline"]} != set(AGENT_NAMES):
        raise ContractError("GitHub preview report Agent inventory is not canonical")
    expected = {
        "report.json": _json_bytes(report),
        "report.md": render_markdown(
            report,
            include_improvement_link=False,
        ).encode("utf-8"),
        **{
            f"agents/{agent_name}.md": render_agent_markdown(
                report,
                agent_name,
            ).encode("utf-8")
            for agent_name in AGENT_NAMES
        },
    }
    for relative, content in expected.items():
        path = report_root / Path(relative)
        if not path.is_file() or path.read_bytes() != content:
            raise ContractError(
                f"GitHub preview artifact is not canonical generated output: {relative}"
            )
        _validate_public_text(content, relative)
    return expected


def _validate_branch(files: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    if not files:
        return {}
    grouped: dict[str, dict[str, bytes]] = {}
    for path, content in files.items():
        validated = _validated_tree_path(path)
        parts = PurePosixPath(validated).parts
        if len(parts) not in {2, 3}:
            raise ContractError("GitHub preview branch contains an unmanaged path")
        run_id = parts[0]
        _validate_run_id(run_id)
        relative = "/".join(parts[1:])
        if relative not in _allowed_run_paths():
            raise ContractError("GitHub preview branch contains an unmanaged path")
        grouped.setdefault(run_id, {})[relative] = content
    manifests: dict[str, dict[str, Any]] = {}
    expected_paths = _allowed_run_paths()
    for run_id, run_files in grouped.items():
        if set(run_files) != expected_paths:
            raise ContractError("GitHub preview run has an incomplete artifact set")
        try:
            manifest = json.loads(run_files[".aiq-preview.json"])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractError("GitHub preview manifest is invalid JSON") from error
        if (
            not isinstance(manifest, dict)
            or run_files[".aiq-preview.json"] != _json_bytes(manifest)
        ):
            raise ContractError("GitHub preview manifest is not canonical")
        _validate_manifest(manifest, run_id, run_files)
        for relative in expected_paths - {".aiq-preview.json"}:
            _validate_public_text(run_files[relative], relative)
        manifests[run_id] = manifest
    return manifests


def _build_manifest(
    run_id: str,
    artifacts: Mapping[str, bytes],
    created_at: datetime,
) -> dict[str, Any]:
    entries = [
        {"path": path, "content_digest": _byte_digest(content)}
        for path, content in sorted(artifacts.items())
    ]
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-email-test-preview",
        "repository": REPOSITORY,
        "branch": PREVIEW_BRANCH,
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "format": "daily-report-v3-markdown-v1",
        "content_digest": content_hash({"artifacts": entries}),
        "artifacts": entries,
    }
    _validate_manifest(
        value,
        run_id,
        {**artifacts, ".aiq-preview.json": _json_bytes(value)},
    )
    return value


def _assert_published_run(
    files: Mapping[str, bytes],
    manifests: Mapping[str, Mapping[str, Any]],
    expected: Mapping[str, bytes],
    publication: Mapping[str, Any],
) -> None:
    run_id = str(publication["run_id"])
    if run_id not in manifests:
        raise ContractError("GitHub preview publication run is missing")
    prefix = f"{run_id}/"
    actual = {
        path.removeprefix(prefix): content
        for path, content in files.items()
        if path.startswith(prefix) and path != f"{run_id}/.aiq-preview.json"
    }
    manifest = manifests[run_id]
    if (
        actual != expected
        or manifest["content_digest"] != publication["content_digest"]
        or _byte_digest(_json_bytes(manifest)) != publication["manifest_digest"]
    ):
        raise ContractError("GitHub preview publication content is divergent")


def _validate_manifest(
    value: Any,
    run_id: str,
    files: Mapping[str, bytes],
) -> None:
    errors = sorted(
        Draft202012Validator(
            read_json(_MANIFEST_SCHEMA),
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ContractError(f"GitHub preview manifest is invalid: {errors[0].message}")
    if value["run_id"] != run_id:
        raise ContractError("GitHub preview manifest run binding is invalid")
    entries = [
        {"path": path, "content_digest": _byte_digest(files[path])}
        for path in sorted(_allowed_run_paths() - {".aiq-preview.json"})
    ]
    if value["artifacts"] != entries or value["content_digest"] != content_hash(
        {"artifacts": entries}
    ):
        raise ContractError("GitHub preview manifest content binding is invalid")


def _publication(
    manifest: Mapping[str, Any],
    *,
    commit_sha: str,
) -> dict[str, Any]:
    links = preview_links(str(manifest["run_id"]))
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-email-test-preview-publication",
        **links,
        "created_at": manifest["created_at"],
        "commit_sha": _validated_sha(commit_sha),
        "content_digest": manifest["content_digest"],
        "manifest_digest": _byte_digest(_json_bytes(manifest)),
    }
    validate_preview_publication(value, run_id=str(manifest["run_id"]))
    return value


def _verify_links(git: GitHubGitApi, publication: Mapping[str, Any]) -> None:
    run_id = str(publication["run_id"])
    git.verify_file(f"{run_id}/report.md")
    for agent_name in AGENT_NAMES:
        git.verify_file(f"{run_id}/agents/{agent_name}.md")


def _validate_public_text(content: bytes, label: str) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"GitHub preview artifact is not UTF-8: {label}") from error
    normalized_text = html.unescape(text)
    if any(
        pattern.search(normalized_text)
        for pattern in (*_FORBIDDEN_PUBLIC_TEXT, _PRIVATE_IDENTIFIER_PATTERN)
    ):
        raise ContractError(f"GitHub preview artifact contains private data: {label}")
    destinations = [
        *_URL_PATTERN.findall(normalized_text),
        *_MARKDOWN_LINK_PATTERN.findall(normalized_text),
        *_HTML_LINK_PATTERN.findall(normalized_text),
    ]
    for destination in destinations:
        normalized_url = (
            f"https:{destination}"
            if destination.startswith("//")
            else destination
        )
        parsed = urlsplit(normalized_url)
        if (parsed.scheme or parsed.netloc) and not normalized_url.startswith(
            _PUBLIC_REPOSITORY_URL
        ):
            raise ContractError(f"GitHub preview artifact contains a private link: {label}")


def _allowed_run_paths() -> set[str]:
    return {
        ".aiq-preview.json",
        "report.json",
        "report.md",
        *(f"agents/{agent_name}.md" for agent_name in AGENT_NAMES),
    }


def _validate_run_id(run_id: str) -> None:
    match = _RUN_PATTERN.fullmatch(run_id)
    if match is None or int(match.group(1)) <= 0:
        raise ContractError("GitHub preview requires a nonzero Daily test rerun identity")


def _validated_tree_path(value: Any) -> str:
    path = str(value or "")
    if (
        not path.isascii()
        or "\\" in path
        or path.startswith("/")
        or ".." in PurePosixPath(path).parts
        or PurePosixPath(path).as_posix() != path
    ):
        raise ContractError("GitHub preview path is invalid")
    return path


def _validated_sha(value: Any) -> str:
    sha = str(value or "")
    if _SHA_PATTERN.fullmatch(sha) is None:
        raise ContractError("GitHub preview Git object identity is invalid")
    return sha


def _byte_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("ascii")
