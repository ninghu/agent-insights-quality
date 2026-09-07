"""Read-only validation of historical public reports; never a Daily publisher."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.errors import QualityError
from agent_insights_quality.privacy import restore_public_result
from agent_insights_quality.report_context import ReportMetadata, load_report_context
from agent_insights_quality.reporting import render_markdown
from agent_insights_quality.results import PlannedUnit
from agent_insights_quality.results import QualityResult
from agent_insights_quality.selection import select_daily


def _plan(root: Path, report_date: date) -> tuple[PlannedUnit, ...]:
    return tuple(
        PlannedUnit(item.unit_id, None if item.is_baseline else item.unit_id.logical_version)
        for item in select_daily(load_catalog(root), report_date)
    )


def approved_document(
    root: Path, document: Mapping[str, Any],
) -> tuple[dict[str, Any], QualityResult, tuple[PlannedUnit, ...]]:
    from agent_insights_quality.publication import validate_public_report

    try:
        report_date = date.fromisoformat(document["report_date"])
    except (KeyError, ValueError, TypeError) as error:
        raise QualityError("public_report_date_invalid") from error
    plan = _plan(root, report_date)
    value = validate_public_report(document, allowed_units=plan)
    result = restore_public_result(value["report"], allowed_units=plan)
    return value, result, plan


def public_markdown(root: Path, document: Mapping[str, Any]) -> str:
    value, result, plan = approved_document(root, document)
    return render_markdown(
        result, allowed_units=plan,
        report_context=load_report_context(root, allowed_units=plan),
        metadata=ReportMetadata(value["report_date"], value["region"], value["source_commit"]),
        delivery_id=value["framework_run_id"],
    )


def verify_artifact(
    root: Path, json_path: Path, markdown_path: Path | None = None,
    repository_path: str | None = None,
) -> None:
    if json_path.stat().st_size > 10 * 1024 * 1024:
        raise QualityError("public_report_too_large")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate public field")
            value[key] = item
        return value

    try:
        value = json.loads(json_path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise QualityError("public_report_invalid") from error
    value, _, _ = approved_document(root, value)
    if repository_path is not None and repository_path != "reports/latest.json":
        match = re.fullmatch(r"reports/daily/(\d{4})/(\d{2})/(\d{2})/report.json", repository_path)
        if match is None or "-".join(match.groups()) != value["report_date"]:
            raise QualityError("public_report_path_mismatch")
    trusted = subprocess.run(
        ["git", "merge-base", "--is-ancestor", value["source_commit"], "HEAD"],
        cwd=root, capture_output=True, check=False,
    )
    if trusted.returncode:
        raise QualityError("public_report_source_untrusted")
    if markdown_path is not None and markdown_path.read_text(encoding="utf-8") != public_markdown(root, value):
        raise QualityError("public_report_rendering_mismatch")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--repository-path")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    verify_artifact(root, arguments.report, arguments.markdown, arguments.repository_path)
    print("Public report artifact is consistent.")


if __name__ == "__main__":
    main()
