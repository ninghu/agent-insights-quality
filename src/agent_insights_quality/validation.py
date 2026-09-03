from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agent_insights_quality.catalogs import generate_docs, load_catalogs
from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.scoring import (
    QUALITY_SCORE_FORMULA,
    SCORING_FIELDS,
)
from agent_insights_quality.selection import DAILY_ISSUES_PER_AGENT
from agent_insights_quality.util import ROOT, ContractError, read_yaml
from agent_insights_quality.validation_policy import load_validation_policy

_REMOVED_TERMS = re.compile(
    r"(?i)\b" + "s" + r"cn\b|aiq-" + "s" + "cn"
)
def validate_repository() -> None:
    _, issues = load_catalogs()
    policy = load_automation_policy()
    if (
        issues["selection"]["issues_per_agent_daily"]
        != policy.issues_per_agent_daily
        or policy.issues_per_agent_daily != DAILY_ISSUES_PER_AGENT
    ):
        raise ContractError("Daily selection contracts are inconsistent")
    generate_docs(check=True)
    _validate_reporting_policy()
    _validate_removed_terms()
    _validate_sensitive_content()
    _validate_test_agent_validation_boundary()


def _tracked_text_files() -> list[Path]:
    values: set[Path] = set()
    process = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if process.returncode == 0:
        for raw in process.stdout.split(b"\0"):
            if not raw:
                continue
            path = ROOT / raw.decode("utf-8")
            if path.is_file():
                values.add(path)
    for path in ROOT.rglob("*"):
        if (
            path.is_file()
            and ".git" not in path.parts
            and ".aiq-runtime" not in path.parts
            and ".mypy_cache" not in path.parts
            and ".pytest_cache" not in path.parts
            and ".ruff_cache" not in path.parts
            and "__pycache__" not in path.parts
            and path.suffix.lower() not in {".pyc", ".zip", ".png", ".jpg", ".jpeg"}
            and path.stat().st_size <= 2 * 1024 * 1024
        ):
            values.add(path)
    return sorted(values)


def _validate_reporting_policy() -> None:
    policy = read_yaml(ROOT / "config" / "reporting.yaml")
    if policy.get("recipient") != "agentinsightsteam@microsoft.com":
        raise ContractError("Reporting recipient is not the reviewed team mailbox")
    if policy.get("email_channel") != "copilot_email":
        raise ContractError("Reporting email channel is not reviewed")
    if policy.get("dashboard_url") != "https://aka.ms/agent-insights/quality":
        raise ContractError("Reporting dashboard link is not reviewed")
    if policy.get("quality_score") != {
        "formula": QUALITY_SCORE_FORMULA,
        "scoring_fields": list(SCORING_FIELDS),
        "diagnostic_fields": ["severity", "proposed_fix"],
    }:
        raise ContractError("Reporting quality-score policy does not match implementation")


def _validate_removed_terms() -> None:
    violations = []
    for path in _tracked_text_files():
        content = path.read_text(encoding="utf-8", errors="replace")
        if _REMOVED_TERMS.search(content):
            violations.append(path.relative_to(ROOT).as_posix())
    if violations:
        raise ContractError(
            "Removed identifier vocabulary remains in: " + ", ".join(violations)
        )


def _validate_sensitive_content() -> None:
    policy_path = ROOT / "config" / "security.yaml"
    policy = read_yaml(policy_path)
    patterns = [
        re.compile(value)
        for value in policy.get("forbidden_tracked_patterns", [])
        if isinstance(value, str)
    ]
    violations = []
    for path in _tracked_text_files():
        if path == policy_path:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if any(pattern.search(content) for pattern in patterns):
            violations.append(path.relative_to(ROOT).as_posix())
    if violations:
        raise ContractError(
            "Potential private or sensitive values found in: " + ", ".join(violations)
        )


def _validate_test_agent_validation_boundary() -> None:
    policy = load_validation_policy()
    if (
        policy.authority_count != 41
        or policy.environment_id != "swedencentral-g30"
        or policy.location != "swedencentral"
        or policy.project_name != "aiq-staging-swedencentral"
        or policy.telemetry_resource_set != "g30"
    ):
        raise ContractError("Test Agent Validation config is not reviewed")
    forbidden = (
        "agent_insights_quality.adx",
        "agent_insights_quality.assessment",
        "agent_insights_quality.email",
        "agent_insights_quality.reporting",
        "ensure_monitor(",
        "start_insights_run(",
        "publish_daily_report",
    )
    violations = []
    for path in sorted(
        (ROOT / "src" / "agent_insights_quality").glob("validation_*.py")
    ):
        content = path.read_text(encoding="utf-8")
        if any(value in content for value in forbidden):
            violations.append(path.relative_to(ROOT).as_posix())
    removed_paths = (
        ROOT / ".github" / "workflows" / "test-agent-validation.yml",
        ROOT / ".github" / "workflows" / "test-agent-validation-receipt.yml",
        ROOT / ".github" / "workflows" / "test-agent-validation-reconciler.yml",
        ROOT / ".github" / "workflows" / "test-agent-validation-review.yml",
        ROOT / "src" / "agent_insights_quality" / "validation_gate.py",
        ROOT / "src" / "agent_insights_quality" / "validation_issuer.py",
        ROOT / "schemas" / "test-agent-validation-receipt.schema.json",
    )
    violations.extend(
        path.relative_to(ROOT).as_posix()
        for path in removed_paths
        if path.exists()
    )
    if violations:
        raise ContractError(
            "Test Agent Validation crosses its report-free boundary: "
            + ", ".join(violations)
        )
