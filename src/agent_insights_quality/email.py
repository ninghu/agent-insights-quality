"""Immutable private email requests and app-native claim/outcome handoff.

This module cannot send email. The app must claim first, send the exact returned
recipient/subject/HTML once, then record the actual provider result. A claimed or
unknown outcome needs reconciliation, never a blind resend. Hold the enclosing
RuntimeStore.ownership() during each operation; all writes use its RecordStore.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date
import json
import re
import threading
from typing import Any, Literal

from .errors import QualityError
from .report_context import ReportMetadata, ReviewedReportContext
from .report_links import VerifiedScoringLink
from .reporting import render_email_html
from .results import PlannedUnit, QualityResult
from .state import RecordStore, StateConflict

TEAM_RECIPIENT = "agentinsightsteam@microsoft.com"
SendOutcome = Literal["accepted", "delivered", "rejected", "unknown"]
_LOCK = threading.RLock()
_OUTCOMES = {"accepted", "delivered", "rejected", "unknown"}
_STATUSES = _OUTCOMES | {"prepared", "claimed"}


class EmailError(QualityError):
    """Public-safe handoff error; never include a recipient/provider message."""


def _address(value: str | None) -> str:
    if (
        not isinstance(value, str) or len(value) > 254
        or re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", value,
        ) is None
    ):
        raise EmailError("email_recipient_invalid")
    return value


def _identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value) is None
    ):
        raise EmailError("email_identity_invalid")
    return value


@dataclass(frozen=True)
class EmailRequest:
    delivery_id: str
    recipient: str
    subject: str
    html: str
    mode: Literal["test", "official", "failure"]
    report_date: str
    test_run: bool
    rerun: int
    report_access: dict | None = None
    delivery_binding: dict | None = None

    def to_private_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.report_access is None:
            value.pop("report_access")
        if self.delivery_binding is None:
            value.pop("delivery_binding")
        return value


@dataclass(frozen=True)
class EmailRecord:
    request: EmailRequest
    status: str
    claim_id: str | None = None
    provider_result: dict[str, Any] | None = None

    @property
    def inbox_delivery_confirmed(self) -> bool:
        return self.status == "delivered"

    def to_private_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0", "request": self.request.to_private_dict(),
            "status": self.status, "claim_id": self.claim_id, "provider_result": self.provider_result,
        }


def _read(outbox: RecordStore, delivery_id: str, *, missing_ok: bool = False) -> EmailRecord | None:
    raw = outbox.read(_identifier(delivery_id), missing_ok=missing_ok)
    if raw is None:
        return None
    required = {"schema_version", "request", "status", "claim_id", "provider_result"}
    if (
        set(raw) != required or raw["schema_version"] != "1.0"
        or not isinstance(raw["request"], dict)
        or set(raw["request"]) - {"report_access", "delivery_binding"} != {
            "delivery_id", "recipient", "subject", "html", "mode",
            "report_date", "test_run", "rerun",
        }
        or not isinstance(raw["status"], str) or raw["status"] not in _STATUSES
    ):
        raise EmailError("email_record_invalid")
    request = EmailRequest(**raw["request"])
    _validate_request(request)
    from .automation_launch import validate_email_binding
    validate_email_binding(outbox._runtime, request)
    if request.delivery_id != delivery_id:
        raise EmailError("email_record_invalid")
    claimed = raw["status"] != "prepared"
    if claimed:
        _identifier(raw["claim_id"])
    elif raw["claim_id"] is not None:
        raise EmailError("email_record_invalid")
    if raw["status"] in _OUTCOMES:
        _provider_result(raw["provider_result"])
    elif raw["provider_result"] is not None:
        raise EmailError("email_record_invalid")
    return EmailRecord(request, raw["status"], raw["claim_id"], raw["provider_result"])


def _validate_request(request: EmailRequest) -> None:
    _identifier(request.delivery_id)
    _address(request.recipient)
    try:
        if date.fromisoformat(request.report_date).isoformat() != request.report_date:
            raise ValueError("Date must be canonical")
    except (TypeError, ValueError) as error:
        raise EmailError("email_date_invalid") from error
    if (
        not isinstance(request.mode, str) or request.mode not in {"test", "official", "failure"}
        or type(request.test_run) is not bool or type(request.rerun) is not int
        or request.test_run != (request.mode == "test")
        or (request.rerun < 1 if request.test_run else request.rerun != 0)
        or not isinstance(request.subject, str) or not request.subject
        or not request.subject.isprintable()
        or not isinstance(request.html, str) or not request.html
        or request.mode != "official" and request.recipient.casefold() == TEAM_RECIPIENT
    ):
        raise EmailError("email_request_invalid")
    if request.delivery_binding is None:
        if request.mode == "official" and request.recipient != TEAM_RECIPIENT:
            raise EmailError("email_recipient_isolation")
    else:
        from .automation_launch import validate_launch
        binding = validate_launch(request.delivery_binding)
        if (
            binding["run_id"] != request.delivery_id
            or binding["report_date"] != request.report_date
            or binding["rerun"] != request.rerun
            or (binding["report_mode"] == "test") is not request.test_run
            or request.mode != "failure" and binding["to_address"] != request.recipient
        ):
            raise EmailError("email_delivery_binding_invalid")
    if request.report_access is not None:
        from .report_access import validate_descriptor
        validate_descriptor(request.report_access, request.delivery_id)


def render_email_content(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit], report_date: str,
    test_run: bool = False, private_context: str | None = None,
    work_item_context: dict | None = None, warnings: tuple[str, ...] = (),
    report_context: ReviewedReportContext | None = None,
    region_display: str | None = None, source_revision: str | None = None,
    details_href: str | None = None, delivery_id: str | None = None,
    scoring_link: VerifiedScoringLink | None = None,
    agent_links: dict[str, str] | None = None, attached_report: bool = False,
    report_access=None,
) -> tuple[str, str]:
    """Render only: no recipients, claims, delivery records or provider calls."""
    try:
        if date.fromisoformat(report_date).isoformat() != report_date:
            raise ValueError("Date must be canonical")
    except (TypeError, ValueError) as error:
        raise EmailError("email_date_invalid") from error
    if type(test_run) is not bool:
        raise EmailError("email_request_invalid")
    if (region_display is None) != (source_revision is None):
        raise EmailError("email_metadata_incomplete")
    metadata = (
        ReportMetadata(report_date, region_display, source_revision)
        if region_display is not None else None
    )
    html = render_email_html(
        result, allowed_units=allowed_units, warnings=warnings, report_context=report_context,
        metadata=metadata, test_run=test_run, details_href=details_href, delivery_id=delivery_id,
        scoring_link=scoring_link, agent_links=agent_links, attached_report=attached_report,
        report_access=report_access,
    )
    prefix = "[TEST] " if test_run else ""
    summary = (
        f"{result.score:.1f}/100 - {report_date} - "
        f"{result.counts.correct_issues}/{result.counts.expected_issues} issues"
        if result.team_report_eligible else f"Measurement unavailable - {report_date}"
    )
    subject = f"{prefix}[Agent Insights Quality] {summary}"
    if region_display:
        subject += " - " + region_display
    private_sections = []
    if work_item_context is not None:
        from .work_items import render_work_item_html
        fragment = render_work_item_html(work_item_context)
        private_sections.append(
            '<tr><td style="padding:22px 28px 4px;font-family:Segoe UI,Arial,sans-serif;'
            f'font-size:14px;line-height:21px;">{fragment}</td></tr>'
        )
    if private_context is not None:
        if not isinstance(private_context, str):
            raise EmailError("email_private_context_invalid")
        # Retain this legacy input in its checkpoint only, never as hidden HTML
        # or a catch-all notes section. Typed work items are the sole email prose input.
    return subject, html.replace("<!--private-context-->", "".join(private_sections))


def prepare_email(
    outbox: RecordStore, delivery_id: str, result: QualityResult, *,
    allowed_units: Iterable[PlannedUnit], report_date: str,
    test_run: bool = False, rerun: int = 0,
    test_recipient: str | None = None, failure_recipient: str | None = None,
    team_recipient: str = TEAM_RECIPIENT, private_context: str | None = None,
    work_item_context: dict | None = None,
    warnings: tuple[str, ...] = (),
    report_context: ReviewedReportContext | None = None,
    region_display: str | None = None, source_revision: str | None = None,
    scoring_link: VerifiedScoringLink | None = None, agent_links: dict[str, str] | None = None,
    report_access=None, delivery_binding: dict | None = None,
    presentation_only: bool = False,
) -> EmailRequest:
    """Prepare one private record (including the HTML preview), without sending.

    Replaying identical content is safe. A changed recipient/content/mode/date
    under an existing delivery identity is a conflict, including after delivery.
    Test mode cannot select the team recipient or invoke any public sink.
    """
    if type(presentation_only) is not bool or presentation_only and not test_run:
        raise EmailError("email_presentation_requires_test")
    team = _address(team_recipient)
    if team != TEAM_RECIPIENT:
        raise EmailError("email_recipient_isolation")
    if delivery_binding is not None:
        from .automation_launch import validate_launch
        validate_launch(delivery_binding)
        if source_revision is not None and source_revision != delivery_binding["source_revision"]:
            raise EmailError("email_delivery_binding_invalid")
    mode = "test" if test_run else "official" if result.team_report_eligible else "failure"
    recipient = _address(
        test_recipient if mode == "test" else (
            delivery_binding["to_address"] if delivery_binding is not None else team
        ) if mode == "official" else failure_recipient
    )
    if mode != "official" and recipient.casefold() in {team.casefold(), TEAM_RECIPIENT}:
        raise EmailError("email_recipient_isolation")
    subject, html = render_email_content(
        result, allowed_units=allowed_units, report_date=report_date, test_run=test_run,
        private_context=private_context, work_item_context=work_item_context, warnings=warnings,
        report_context=report_context, region_display=region_display, source_revision=source_revision,
        delivery_id=delivery_id,
        scoring_link=scoring_link, agent_links=agent_links,
        report_access=report_access,
    )
    if presentation_only:
        subject = "[PRESENTATION UPDATE] " + subject
    request = EmailRequest(
        delivery_id, recipient, subject, html, mode, report_date, test_run, rerun,
        dict(report_access.descriptor) if report_access is not None else None,
        delivery_binding,
    )
    _validate_request(request)
    from .automation_launch import validate_email_binding
    validate_email_binding(outbox._runtime, request)
    with _LOCK:
        previous = _read(outbox, delivery_id, missing_ok=True)
        if previous is not None:
            if previous.request != request:
                raise StateConflict()
            # Even an identical replay must establish durability under ownership.
            outbox.save_progress(delivery_id, previous.to_private_dict())
            return previous.request
        outbox.save_progress(delivery_id, EmailRecord(request, "prepared").to_private_dict())
    return request


def claim_email(
    outbox: RecordStore, delivery_id: str, *, claim_id: str,
) -> EmailRequest:
    """Persist the claim before returning sendable content, exactly once."""
    _identifier(claim_id)
    with _LOCK:
        record = _read(outbox, delivery_id)
        assert record is not None
        if record.status != "prepared":
            if record.claim_id != claim_id:
                raise EmailError("email_claim_conflict")
            if record.status in {"claimed", "unknown"}:
                raise EmailError("email_reconciliation_required")
            raise EmailError("email_already_finalized")
        if record.request.report_access is not None:
            from .report_access import read_report_access
            access = read_report_access(
                outbox._runtime, record.request.report_access, delivery_id=delivery_id,
            )
            if access.expired():
                raise EmailError("email_report_access_expired_needs_new_revision")
        claimed = EmailRecord(record.request, "claimed", claim_id)
        outbox.save_progress(delivery_id, claimed.to_private_dict())
        return record.request


def _provider_result(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise EmailError("email_provider_result_required")
    try:
        def keys(item: Any) -> None:
            if isinstance(item, Mapping):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("Provider result keys must be strings")
                for child in item.values():
                    keys(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    keys(child)
        keys(value)
        return json.loads(json.dumps(dict(value), allow_nan=False))
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise EmailError("email_provider_result_invalid") from error


def record_email_outcome(
    outbox: RecordStore, delivery_id: str, *, claim_id: str,
    outcome: SendOutcome, provider_result: Mapping[str, Any],
    reconciliation: bool = False,
) -> EmailRecord:
    """Record caller-supplied actual provider evidence, not a claimed success.

    ``accepted`` never means inbox delivery. ``delivered`` requires an actual
    delivery-confirming result from the caller. Reconciliation records evidence
    about the existing send; it does not authorize another send.
    """
    _identifier(claim_id)
    if (
        not isinstance(outcome, str) or outcome not in _OUTCOMES
        or type(reconciliation) is not bool
    ):
        raise EmailError("email_outcome_invalid")
    result = _provider_result(provider_result)
    with _LOCK:
        record = _read(outbox, delivery_id)
        assert record is not None
        if record.claim_id != claim_id or record.status == "prepared":
            raise EmailError("email_claim_conflict")
        updated = EmailRecord(record.request, outcome, claim_id, result)
        if record == updated:
            outbox.save_progress(delivery_id, updated.to_private_dict())
            return updated
        if record.status != "claimed":
            if not reconciliation:
                raise EmailError("email_reconciliation_required")
            if not (
                record.status == "unknown" and outcome in {"accepted", "delivered", "rejected"}
                or record.status == "accepted" and outcome == "delivered"
            ):
                raise EmailError("email_outcome_conflict")
        outbox.save_progress(delivery_id, updated.to_private_dict())
        return updated


def read_email(outbox: RecordStore, delivery_id: str) -> EmailRecord:
    """Read for reconciliation only; this never authorizes a send."""
    record = _read(outbox, delivery_id)
    assert record is not None
    return record
