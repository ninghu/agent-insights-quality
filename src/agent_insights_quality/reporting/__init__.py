from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from agent_insights_quality.contracts import (
    ContractError,
    ROOT,
    SCHEMAS,
    load_data,
    validate_instance,
    validate_reporting_config,
)
from agent_insights_quality.readiness import (
    READINESS_FAILURE_PROHIBITED_ACTIONS,
    incomplete_runtime_components,
)


READINESS_FAILURE_ARTIFACTS = (
    "readiness-failure.json",
    "readiness-failure.md",
    "failure-email.html",
    "email-handoff.json",
)


def _validated_report_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ContractError(f"Invalid report date: {value}") from error
    if parsed.isoformat() != value:
        raise ContractError(f"Invalid report date: {value}")
    return parsed


def _default_generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_delivery_result(delivery: dict[str, Any], label: str) -> None:
    if delivery["status"] == "pending" and (
        delivery["receipt_reference"] is not None or delivery["error_code"] is not None
    ):
        raise ContractError(f"{label}: pending delivery cannot record a result")
    if delivery["status"] == "sent" and (
        delivery["receipt_reference"] is None or delivery["error_code"] is not None
    ):
        raise ContractError(f"{label}: sent delivery requires only a receipt reference")
    if delivery["status"] == "failed" and (
        delivery["receipt_reference"] is not None or delivery["error_code"] is None
    ):
        raise ContractError(f"{label}: failed delivery requires only an error code")


def validate_email_handoff(
    handoff: dict[str, Any],
    label: str,
    reporting: dict[str, Any],
    *,
    require_current_selection: bool = False,
) -> None:
    validate_reporting_config(reporting)
    validate_instance(handoff, SCHEMAS / "email-handoff.schema.json", label)
    recorded_mode = handoff["reporting_mode"]
    if handoff["recipient_variable"] != reporting["recipient_variables"][recorded_mode]:
        raise ContractError(f"{label}: recipient variable does not match recorded reporting mode")
    if require_current_selection and recorded_mode != reporting["mode"]:
        raise ContractError(f"{label}: reporting mode does not match config/reporting.yaml")
    if require_current_selection and handoff["recipient_variable"] != reporting["recipient_variable"]:
        raise ContractError(f"{label}: recipient variable does not match current reporting mode")
    if handoff["allowed_domain"] != reporting["allowed_domain"]:
        raise ContractError(f"{label}: allowed domain does not match config/reporting.yaml")
    _validate_delivery_result(handoff["delivery"], label)


def _bundle_content_digest(contents: dict[str, str], subject: str) -> str:
    digest = hashlib.sha256()
    digest_inputs = dict(contents)
    digest_inputs["email-subject.txt"] = subject + "\n"
    for filename in sorted(digest_inputs):
        digest.update(filename.encode("ascii"))
        digest.update(b"\0")
        digest.update(digest_inputs[filename].encode("ascii"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _stored_bundle_content(target: Path) -> dict[str, str]:
    filenames = ("readiness-failure.json", "readiness-failure.md", "failure-email.html")
    missing = [filename for filename in filenames if not (target / filename).is_file()]
    if missing:
        raise ContractError(
            f"{target}: incomplete readiness failure bundle: {', '.join(missing)}"
        )
    return {
        filename: (target / filename).read_text(encoding="ascii")
        for filename in filenames
    }


def validate_stored_bundle_content(
    handoff_path: Path,
    handoff: dict[str, Any],
) -> None:
    actual = _bundle_content_digest(
        _stored_bundle_content(handoff_path.parent),
        handoff["subject"],
    )
    if handoff["content_digest"] != actual:
        raise ContractError(f"{handoff_path}: content digest does not match rendered artifacts")


def record_email_delivery(
    handoff_path: Path,
    *,
    status: str,
    receipt_reference: str | None = None,
    error_code: str | None = None,
) -> None:
    handoff = json.loads(handoff_path.read_text(encoding="ascii"))
    reporting = load_data(ROOT / "config" / "reporting.yaml")
    validate_email_handoff(
        handoff,
        str(handoff_path),
        reporting,
        require_current_selection=True,
    )
    validate_stored_bundle_content(handoff_path, handoff)
    if handoff["delivery"]["status"] != "pending":
        raise ContractError(f"{handoff_path}: delivery result is already recorded")
    handoff["delivery"] = {
        "status": status,
        "receipt_reference": receipt_reference,
        "error_code": error_code,
    }
    validate_email_handoff(
        handoff,
        str(handoff_path),
        reporting,
        require_current_selection=True,
    )
    handoff_path.write_text(json.dumps(handoff, indent=2) + "\n", encoding="ascii")


def finalize_readiness_failure(
    readiness: dict[str, Any],
    reporting: dict[str, Any],
    report_date: str,
    *,
    output_root: Path | None = None,
    generated_at: str | None = None,
) -> Path:
    missing = incomplete_runtime_components(readiness)
    if not missing:
        raise ContractError("Readiness failure finalization requires an incomplete runtime")
    validate_reporting_config(reporting)
    parsed_date = _validated_report_date(report_date)
    generated_at = generated_at or _default_generated_at()
    report_id = f"aiq-{parsed_date:%Y%m%d}"
    target = (output_root or ROOT / "reports") / "daily" / parsed_date.strftime("%Y/%m/%d")
    handoff_path = target / "email-handoff.json"
    existing_handoff = None
    if handoff_path.exists():
        existing_handoff = json.loads(handoff_path.read_text(encoding="ascii"))
        validate_email_handoff(existing_handoff, str(handoff_path), reporting)
        validate_stored_bundle_content(handoff_path, existing_handoff)
        existing_report = json.loads(
            (target / "readiness-failure.json").read_text(encoding="ascii")
        )
        validate_instance(
            existing_report,
            SCHEMAS / "readiness-failure.schema.json",
            str(target / "readiness-failure.json"),
        )
        generated_at = existing_report["generated_at"]
    reason = "Daily runtime components are incomplete: " + ", ".join(missing) + "."
    subject = f"[Agent Insights Quality] INCONCLUSIVE - {report_date} - runtime incomplete"

    failure_report = {
        "schema_version": "1.0.0",
        "report_id": report_id,
        "report_date": report_date,
        "generated_at": generated_at,
        "status": "INCONCLUSIVE",
        "failed_phase": "runtime_readiness",
        "last_confirmed_stage": "repository_contracts_loaded",
        "reason": reason,
        "incomplete_components": missing,
        "prohibited_actions": list(READINESS_FAILURE_PROHIBITED_ACTIONS),
        "performed_actions": [
            "render_failure_report",
            "render_failure_email",
            "create_email_handoff",
        ],
        "diagnostics_reference": "config/runtime-readiness.yaml",
        "safe_next_action": (
            "Complete and human-review every runtime component before enabling qualification."
        ),
        "email_handoff_reference": "email-handoff.json",
    }
    markdown = "\n".join(
        [
            f"# Agent Insights Quality: {report_id}",
            "",
            f"**Status:** INCONCLUSIVE",
            f"**Report date:** {report_date}",
            f"**Failed phase:** Runtime readiness",
            "",
            reason,
            "",
            "No Azure deployment, agent traffic, Agent Insights, ADO, memory transition, "
            "resource cleanup, or generated PR mutation was performed.",
            "",
            "A single direct-email handoff is required. Delivery remains pending until Copilot sends "
            "the rendered email and records a receipt or failure result.",
            "",
        ]
    )
    html = "".join(
        [
            "<!doctype html><html><body>",
            "<h1>Agent Insights Quality: INCONCLUSIVE</h1>",
            f"<p><strong>Report date:</strong> {escape(report_date)}</p>",
            "<h2>Summary</h2>",
            f"<p>{escape(reason)}</p>",
            "<h2>What we are doing well</h2>",
            "<p>The readiness gate prevented all operational and repository mutation phases.</p>",
            "<h2>Gaps and regressions</h2>",
            "<p>The daily runtime is incomplete, so no quality conclusion can be produced.</p>",
            "<h2>Test agents and Agent Insights links</h2>",
            "<p>No agents were deployed and no private links were generated.</p>",
            "</body></html>\n",
        ]
    )
    contents = {
        "readiness-failure.json": json.dumps(failure_report, indent=2) + "\n",
        "readiness-failure.md": markdown,
        "failure-email.html": html,
    }
    email_handoff = {
        "schema_version": "1.0.0",
        "report_id": report_id,
        "report_date": report_date,
        "required": True,
        "message_count": 1,
        "reporting_mode": reporting["mode"],
        "recipient_variable": reporting["recipient_variable"],
        "allowed_domain": reporting["allowed_domain"],
        "sender_context": "authenticated_user_mailbox",
        "transport": "copilot_connected_microsoft_mail",
        "subject": subject,
        "html_artifact": "failure-email.html",
        "content_digest": _bundle_content_digest(contents, subject),
        "delivery": {
            "status": "pending",
            "receipt_reference": None,
            "error_code": None,
        },
    }
    if existing_handoff is not None:
        expected_contract = {key: value for key, value in email_handoff.items() if key != "delivery"}
        existing_contract = {
            key: value for key, value in existing_handoff.items() if key != "delivery"
        }
        if existing_contract != expected_contract:
            raise ContractError(f"{handoff_path}: existing handoff content does not match this run")
        return handoff_path

    validate_instance(
        failure_report,
        SCHEMAS / "readiness-failure.schema.json",
        "readiness failure report",
    )
    validate_email_handoff(
        email_handoff,
        "readiness failure email handoff",
        reporting,
        require_current_selection=True,
    )

    target.mkdir(parents=True, exist_ok=True)
    for filename, content in contents.items():
        (target / filename).write_text(content, encoding="ascii")
    handoff_path.write_text(json.dumps(email_handoff, indent=2) + "\n", encoding="ascii")
    return handoff_path
from agent_insights_quality.reporting.render import (
    create_email_send_request,
    import_email_receipt,
    render_email_html,
    render_report_markdown,
    render_trend,
    resolve_recipient,
    validate_report_consistency,
)
