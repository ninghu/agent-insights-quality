from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agent_insights_quality.catalogs import generate_docs, load_catalogs
from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.reporting import (
    CLEAN_CARD_PRECISION_WEIGHT,
    FIELD_QUALITY_WEIGHT,
    FIELD_WEIGHTS,
    QUALITY_SCORE_FORMULA,
    QUALITY_SCORE_THRESHOLD,
)
from agent_insights_quality.util import ROOT, ContractError, read_yaml

_REMOVED_TERMS = re.compile(
    r"(?i)\b(?:" + "s" + r"cenario|" + "s" + r"cn)\b|aiq-" + "s" + "cn"
)
def validate_repository() -> None:
    load_catalogs()
    load_automation_policy()
    generate_docs(check=True)
    _validate_reporting_policy()
    _validate_removed_terms()
    _validate_sensitive_content()


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
    if policy.get("quality_score") != {
        "formula": QUALITY_SCORE_FORMULA,
        "field_quality_weight": FIELD_QUALITY_WEIGHT,
        "clean_card_precision_weight": CLEAN_CARD_PRECISION_WEIGHT,
        "field_weights": FIELD_WEIGHTS,
        "pass_threshold": QUALITY_SCORE_THRESHOLD,
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
