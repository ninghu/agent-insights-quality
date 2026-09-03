from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import (
    ROOT,
    ContractError,
)
from agent_insights_quality.validation_credentials import (
    LocalAzureOperator,
)
from agent_insights_quality.validation_quota import (
    CapacityPlan,
)


@dataclass(frozen=True)
class LocalGitContext:
    repository: str
    pr_number: int
    commit_sha: str


def discover_github_user() -> str:
    value = _run_json(["gh", "api", "user"], "GitHub user")
    login = str(value.get("login") or "")
    if not login:
        raise ContractError("Authenticated GitHub user identity is missing")
    return login


def discover_local_git_context() -> LocalGitContext:
    commit_sha = current_clean_commit()
    repository_value = _run_json(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        "GitHub repository",
    )
    repository = str(repository_value.get("nameWithOwner") or "")
    if (
        len(repository.split("/")) != 2
        or any(not part or part.strip() != part for part in repository.split("/"))
    ):
        raise ContractError("GitHub repository identity is invalid")
    matching_pulls: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    page = 1
    while True:
        pulls = _run_json_array(
            [
                "gh",
                "api",
                "--method",
                "GET",
                (
                    f"repos/{repository}/commits/{commit_sha}/pulls"
                    f"?per_page=100&page={page}"
                ),
                "--header",
                "Accept: application/vnd.github+json",
                "--header",
                "X-GitHub-Api-Version: 2022-11-28",
            ],
            "GitHub pull requests",
        )
        for pull in pulls:
            number = pull.get("number")
            state = pull.get("state")
            head = pull.get("head")
            head_sha = head.get("sha") if isinstance(head, dict) else None
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 1
                or state not in {"open", "closed"}
                or not isinstance(head, dict)
                or not isinstance(head_sha, str)
                or not _git_sha(head_sha)
                or number in seen_numbers
            ):
                raise ContractError("GitHub pull request response is invalid")
            seen_numbers.add(number)
            if state == "open" and head_sha == commit_sha:
                matching_pulls.append(pull)
        if len(pulls) < 100:
            break
        page += 1
    if len(matching_pulls) != 1:
        raise ContractError(
            "Current clean commit must be the exact head of exactly one open "
            "pull request"
        )
    pr_number = matching_pulls[0]["number"]
    return LocalGitContext(
        repository=repository,
        pr_number=pr_number,
        commit_sha=commit_sha,
    )


def current_clean_commit() -> str:
    _assert_repository_root()
    status = _run_text(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        "Git worktree status",
    )
    if status:
        raise ContractError("Local Test Agent Validation requires a clean worktree")
    commit_sha = _run_text(["git", "rev-parse", "HEAD"], "Git commit")
    if not _git_sha(commit_sha):
        raise ContractError("Local Git commit identity is invalid")
    return commit_sha


def current_tree_sha(commit_sha: str) -> str:
    _assert_repository_root()
    if not _git_sha(commit_sha):
        raise ContractError("Local Git commit identity is invalid")
    tree_sha = _run_text(
        ["git", "rev-parse", f"{commit_sha}^{{tree}}"],
        "Git tree",
    )
    if not _git_sha(tree_sha):
        raise ContractError("Local Git tree identity is invalid")
    return tree_sha


def _capacity_from_lifecycle(active: dict[str, Any]) -> CapacityPlan:
    value = active.get("capacity")
    if not isinstance(value, dict):
        raise ContractError(
            "Retained validation capacity plan is missing"
        )
    try:
        plan = CapacityPlan(**value)
    except TypeError as error:
        raise ContractError(
            "Retained validation capacity plan is invalid"
        ) from error
    if plan.plan_digest != active["digests"]["quota_plan_digest"]:
        raise ContractError(
            "Retained validation capacity digest changed"
        )
    return plan


def _substrate(
    operator: LocalAzureOperator,
    profile: RuntimeProfile,
) -> dict[str, str]:
    values = {
        "tenant_id": operator.tenant_id,
        "subscription_id": operator.subscription_id,
        "account_name": profile.account_name,
        "account_resource_id": profile.account_resource_id,
        "registry_name": profile.container_registry_name,
        "storage_account_name": profile.registry_storage_account_name,
        "telemetry_resource_id": profile.application_insights_resource_id,
    }
    if not all(values.values()):
        raise ContractError("Validation Azure substrate identity is incomplete")
    subscription_prefix = f"/subscriptions/{operator.subscription_id}/".casefold()
    if not all(
        str(values[field]).casefold().startswith(subscription_prefix)
        for field in ("account_resource_id", "telemetry_resource_id")
    ):
        raise ContractError(
            "Validation resources do not belong to the active Azure subscription"
        )
    return values


def _run_text(arguments: list[str], label: str) -> str:
    process = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=ROOT,
    )
    if process.returncode != 0:
        raise ContractError(f"{label} could not be queried")
    return process.stdout.strip()


def _run_json(arguments: list[str], label: str) -> dict[str, Any]:
    raw = _run_text(arguments, label)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ContractError(f"{label} response is invalid") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} response is not an object")
    return value


def _run_json_array(arguments: list[str], label: str) -> list[dict[str, Any]]:
    raw = _run_text(arguments, label)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ContractError(f"{label} response is invalid") from error
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise ContractError(f"{label} response is not an object array")
    return value


def _git_sha(value: str) -> bool:
    return (
        len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _assert_repository_root() -> None:
    expected = ROOT.resolve()
    root = _run_text(
        ["git", "rev-parse", "--show-toplevel"],
        "Git repository root",
    )
    if Path(root).resolve() != expected:
        raise ContractError("Imported repository root does not match Git")
    ambient = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if (
        ambient.returncode != 0
        or Path(ambient.stdout.strip()).resolve() != expected
    ):
        raise ContractError(
            "Current worktree does not match the imported repository root"
        )
