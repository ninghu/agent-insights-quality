"""Explicit linked TEST presentations from frozen measurements; never qualification."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re

from .contracts import Environment
from .email import prepare_email, read_email
from .email_preview import _frozen_inputs, _work_item_presentation
from .errors import QualityError
from .private_publication import AzurePrivateReportBlob, PrivateReportOutbox, _write_local
from .report_access import prepare_report_access, read_report_access
from .report_context import load_report_context
from .report_links import VerifiedScoringLink, foundry_links
from .state import RuntimeStore, StateError, _encode


class LinkedPresentationError(QualityError):
    """Public-safe presentation delivery error."""


def presentation_email_outbox(runtime: RuntimeStore, presentation_id: str):
    if runtime.environment != "daily" or (
        not isinstance(presentation_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", presentation_id) is None
    ):
        raise LinkedPresentationError("linked_presentation_identity_invalid")
    return runtime.outbox("pemail-" + presentation_id)


def _active_presentation(runtime, source_id):
    active = runtime.outbox("presentation-deliveries").read(source_id, missing_ok=True)
    if active is not None:
        if set(active) != {"source_delivery_id", "presentation_id", "access_revision"} or (
            active["source_delivery_id"] != source_id
            or not isinstance(active["access_revision"], str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", active["access_revision"]) is None
        ):
            raise LinkedPresentationError("linked_presentation_active_invalid")
        presentation_email_outbox(runtime, active["presentation_id"])
    return active


def _select_presentation(runtime, source_id, presentation_id, access_revision):
    if access_revision is not None and (
        not isinstance(access_revision, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", access_revision) is None
    ):
        raise LinkedPresentationError("linked_presentation_access_revision_invalid")
    active = _active_presentation(runtime, source_id)
    if active is not None:
        previous_box = presentation_email_outbox(runtime, active["presentation_id"])
        previous = (
            read_email(previous_box, source_id)
            if previous_box.read(source_id, missing_ok=True) is not None else None
        )
        if active["presentation_id"] != presentation_id:
            if previous is None or previous.status in {"prepared", "claimed", "unknown"}:
                raise LinkedPresentationError("linked_presentation_reconciliation_required")
        else:
            revision = access_revision or active["access_revision"]
            if revision != active["access_revision"] and (
                previous is not None
                or previous_box.read_completed("inputs/" + source_id, missing_ok=True) is not None
            ):
                raise LinkedPresentationError("linked_presentation_access_revision_frozen")
            access_revision = revision
    revision = access_revision or "initial"
    runtime.outbox("presentation-deliveries").save_progress(source_id, {
        "source_delivery_id": source_id, "presentation_id": presentation_id,
        "access_revision": revision,
    })
    return revision


def validate_presentation_email(outbox, source_id, presentation_id):
    record = read_email(outbox, source_id)
    source = read_email(outbox._runtime.outbox("email"), source_id)
    saved = outbox.read_completed("inputs/" + source_id)
    frozen = outbox._runtime.run(source_id).read_completed("delivery-inputs")
    active = _active_presentation(outbox._runtime, source_id)
    if (
        not record.request.test_run or source.status not in {"accepted", "delivered"}
        or record.request.recipient != source.request.recipient
        or saved.get("source_delivery_id") != source_id
        or saved.get("presentation_id") != presentation_id
        or saved.get("measurement_changed") is not False
        or saved.get("source_request_sha256") != sha256(_encode(source.request.to_private_dict())).hexdigest()
        or saved.get("frozen_inputs_sha256") != sha256(_encode(frozen)).hexdigest()
        or saved.get("report_access") != record.request.report_access
        or record.request.report_access is None
        or record.request.report_access["record_key"].split("/")[1] != presentation_id
        or record.request.report_access["record_key"].split("/")[3] != saved.get("access_revision")
        or record.status in {"prepared", "claimed", "unknown"} and (
            active is None or active["presentation_id"] != presentation_id
            or active["access_revision"] != saved.get("access_revision")
        )
    ):
        raise LinkedPresentationError("linked_presentation_email_binding_invalid")


def _status(outbox, source_id, publication, blockers):
    validate_presentation_email(outbox, source_id, publication["presentation_id"])
    record = read_email(outbox, source_id)
    if record.request.report_access is None:
        raise LinkedPresentationError("linked_presentation_access_missing")
    access = read_report_access(
        outbox._runtime, record.request.report_access, delivery_id=source_id,
    )
    if access.expired():
        raise LinkedPresentationError("linked_presentation_access_expired")
    path = outbox.directory / "artifacts" / source_id / "email.html"
    _write_local(outbox._runtime, path, record.request.html.encode("utf-8"))
    return {
        "delivery_id": source_id, "presentation_id": publication["presentation_id"],
        "status": record.status, "email_html_path": str(path),
        "email_record_path": str(outbox._path("progress", source_id)),
        "report_html_path": publication["html_path"],
        "expires_at": access.expires_at, "blockers": list(blockers),
        "measurement_changed": False, "send_performed": False,
    }


def prepare_linked_test_presentation(
    runtime: RuntimeStore, source_id: str, *, root: Path, blob_factory=None,
    access_revision: str | None = None,
) -> dict:
    """Publish a separate frozen presentation and prepare one separately claimable email."""
    if runtime.environment != "daily" or not runtime._owned:
        raise StateError("state_not_owned")
    original = read_email(runtime.outbox("email"), source_id)
    if not original.request.test_run or original.status not in {"accepted", "delivered"}:
        raise LinkedPresentationError("linked_presentation_requires_completed_test")
    restored = _frozen_inputs(runtime, original.request)
    if restored is None:
        raise LinkedPresentationError("linked_presentation_frozen_inputs_missing")
    frozen, plan, result, metadata = restored
    context = load_report_context(root, allowed_units=plan)
    if context.to_private_dict()["units"] != frozen["report_context"]["units"]:
        raise LinkedPresentationError("linked_presentation_reviewed_context_changed")
    environment = Environment(**runtime.run(source_id).read_completed("environment"))
    publisher = PrivateReportOutbox(runtime, source_id).prepare_test_presentation(
        root, result, allowed_units=plan, environment=environment,
        source_revision=metadata.source_revision, report_date=metadata.report_date,
    )
    request = publisher.request()
    access_revision = _select_presentation(
        runtime, source_id, request["presentation_id"], access_revision,
    )
    outbox = presentation_email_outbox(runtime, request["presentation_id"])
    input_key = "inputs/" + source_id
    existing = outbox.read(source_id, missing_ok=True)
    if existing is not None:
        saved = outbox.read_completed(input_key)
        return _status(outbox, source_id, publisher.status(), saved["blockers"])

    client = (blob_factory or AzurePrivateReportBlob)(request["account"])
    try:
        publication = publisher.flush(client)
        if publication["status"] != "delivered":
            raise LinkedPresentationError("linked_presentation_publication_incomplete")
        access = prepare_report_access(publisher, client, revision=access_revision)
    finally:
        client.close()
    if access.expired():
        raise LinkedPresentationError("linked_presentation_access_expired")

    links, blockers = foundry_links(runtime, source_id, plan)
    blockers = list(blockers)
    scoring_link = None
    retained_link = frozen.get("presentation", {}).get("scoring_link")
    if retained_link:
        try:
            scoring_link = VerifiedScoringLink.from_retained(root, retained_link)
        except (QualityError, OSError):
            blockers.append("scoring_link_verification_failed")
    if scoring_link is None:
        blockers.append("scoring_link_publication_required")
    work_items, private_context, work_item_source = _work_item_presentation(
        runtime, original.request, frozen,
    )
    inputs = {
        "source_delivery_id": source_id,
        "source_request_sha256": sha256(_encode(original.request.to_private_dict())).hexdigest(),
        "frozen_inputs_sha256": sha256(_encode(frozen)).hexdigest(),
        "presentation_id": request["presentation_id"],
        "access_revision": access_revision,
        "report_access": dict(access.descriptor),
        "work_item_presentation": work_item_source, "blockers": blockers,
        "measurement_changed": False,
    }
    outbox.save_completed(input_key, inputs)
    prepare_email(
        outbox, source_id, result, allowed_units=plan,
        report_date=metadata.report_date, test_run=True, rerun=original.request.rerun,
        test_recipient=original.request.recipient, private_context=private_context,
        work_item_context=work_items, warnings=tuple(frozen["warnings"]),
        report_context=context, region_display=metadata.region_display,
        source_revision=metadata.source_revision, scoring_link=scoring_link,
        agent_links=links, report_access=access,
        delivery_binding=original.request.delivery_binding,
        presentation_only=True,
    )
    return _status(outbox, source_id, publication, blockers)
