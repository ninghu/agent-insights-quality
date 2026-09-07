"""Private offline exports of immutable delivery requests, never mail handoffs.

Hold RuntimeStore.ownership() for the short local snapshot/export. The current
run's frozen delivery inputs are the only measurement authority for restyling;
no provider, latest-result pointer, claim, or delivery checkpoint is consulted.
"""

from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from .email import EmailRequest, TEAM_RECIPIENT, _address, read_email
from .errors import QualityError
from .privacy import restore_public_result, warning_text
from .report_context import ReportMetadata, load_report_context
from .report_links import VerifiedScoringLink, configured_scoring_link, foundry_links
from .report_review import RetainedReviewContext
from .reporting import markdown_view, render_markdown, render_private_markdown
from .results import PlannedUnit, UnitId
from .state import (
    RuntimeStore, StateConflict, StateError, _atomic_write, _confirm_durable,
    _encode, _inside, _open_snapshot, _parts,
)


class PreviewError(QualityError):
    """A public-safe offline export failure."""


@dataclass(frozen=True)
class EmailPreview:
    delivery_id: str
    presentation_id: str
    directory: Path
    restyled: bool
    blockers: tuple[str, ...] = ()
    rescored: bool = False

    def to_dict(self) -> dict:
        value = {
            "delivery_id": self.delivery_id, "presentation_id": self.presentation_id,
            "status": "local_preview", "restyled": self.restyled,
            "blockers": list(self.blockers),
            **{name + "_path": str(self.directory / filename) for name, filename in (
                ("email_html", "email.html"), ("email_eml", "email.eml"),
                ("report_markdown", "report.md"),
                ("report_html", "report.html"), ("manifest", "manifest.json"),
            )},
        }
        if self.rescored:
            value.update(rescored=True, result_path=str(self.directory / "result.json"))
        return value


class _SafeHTML(HTMLParser):
    """Reject executable markup and auto-loaded resources, without rewriting."""

    def __init__(self, *, local_links: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.local_links = local_links
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        if tag in {
            "script", "iframe", "frame", "frameset", "object", "embed", "base",
            "form", "input", "button", "svg", "math", "link", "video", "audio",
        }:
            raise PreviewError("email_preview_unsafe_html")
        self.in_style = tag == "style" or self.in_style
        for name, value in attrs:
            if name.startswith("on") or name in {
                "src", "srcset", "srcdoc", "background", "action", "formaction",
                "poster", "data", "ping", "http-equiv",
            }:
                raise PreviewError("email_preview_unsafe_html")
            if name == "style" and value is not None:
                self._css(value)
            if name == "href":
                self._link(value)

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            self._css(data)

    def handle_comment(self, data):
        # Outlook renders MSO conditional comments as markup.
        if re.match(r"\s*\[if\b", data, re.I):
            match = re.fullmatch(r"\s*\[if[^\]]+\]>(.*)<!\[endif\]\s*", data, re.I | re.S)
            if match is None:
                raise PreviewError("email_preview_unsafe_html")
            nested = _SafeHTML(local_links=self.local_links)
            nested.feed(match[1])
            nested.close()

    @staticmethod
    def _css(value):
        if re.search(r"url\s*\(|expression\s*\(|@import|\\|/\*", value, re.I):
            raise PreviewError("email_preview_unsafe_html")

    def _link(self, value):
        if (
            not isinstance(value, str) or not value
            or any(character.isspace() or ord(character) < 32 for character in value)
            or "\\" in value
        ):
            raise PreviewError("email_preview_unsafe_link")
        if re.fullmatch(r"#[a-zA-Z][a-zA-Z0-9_-]*", value):
            return
        if self.local_links and re.fullmatch(
            r"report\.html(?:#[a-z][a-z0-9-]*)?", value,
        ):
            return
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)
            ):
                raise ValueError("Unsafe link")
        except ValueError as error:
            raise PreviewError("email_preview_unsafe_link") from error


def _validate_html(value: str, *, local_links: bool) -> None:
    if not isinstance(value, str) or not value:
        raise PreviewError("email_preview_html_invalid")
    parser = _SafeHTML(local_links=local_links)
    parser.feed(value)
    parser.close()


def _frozen_inputs(runtime, request):
    records = runtime.run(request.delivery_id)
    frozen = records.read_completed("delivery-inputs", missing_ok=True)
    if frozen is None:
        return None
    required = {
        "report", "test_run", "rerun", "report_date", "region_display",
        "source_revision", "private_context", "warnings", "recipient", "report_context",
    }
    if (
        not required <= frozen.keys()
        or frozen.keys() - required - {
            "configured_assessor", "work_item_context", "presentation", "delivery_binding",
        }
        or frozen["test_run"] is not request.test_run
        or type(frozen["rerun"]) is not int or frozen["rerun"] != request.rerun
        or frozen["report_date"] != request.report_date
        or not isinstance(frozen["recipient"], str)
        or frozen["private_context"] is not None and not isinstance(frozen["private_context"], str)
        or not isinstance(frozen["warnings"], list)
    ):
        raise PreviewError("email_preview_frozen_invalid")
    if frozen.get("delivery_binding") != request.delivery_binding:
        raise PreviewError("email_preview_delivery_mismatch")
    from .automation_launch import validate_email_binding
    validate_email_binding(runtime, request)
    if _address(frozen["recipient"]).casefold() == TEAM_RECIPIENT:
        raise PreviewError("email_preview_delivery_mismatch")
    if "work_item_context" in frozen:
        from .work_items import validate_work_item_context
        work_items = validate_work_item_context(frozen["work_item_context"])
        if work_items["status"] == "available" and (
            work_items["snapshot"]["report_date"] != request.report_date
        ):
            raise PreviewError("email_preview_delivery_mismatch")
    warning_text(tuple(frozen["warnings"]))
    metadata = ReportMetadata(
        frozen["report_date"], frozen["region_display"], frozen["source_revision"],
    )
    context = frozen["report_context"]
    try:
        if (
            not isinstance(context, dict) or set(context) != {"catalog_root", "units"}
            or not isinstance(context["catalog_root"], str)
            or not isinstance(context["units"], list) or not context["units"]
        ):
            raise ValueError("Invalid reviewed context")
        plan = []
        for unit in context["units"]:
            if not isinstance(unit, dict) or set(unit) != {
                "planned", "title", "expected_symptom", "healthy_behavior",
                "traffic_path", "source_path",
            }:
                raise ValueError("Invalid reviewed unit")
            if any(
                not isinstance(unit[name], str) or not 1 <= len(unit[name]) <= 2000
                or not unit[name].isprintable() or unit[name] != unit[name].strip()
                for name in set(unit) - {"planned"}
            ):
                raise ValueError("Invalid reviewed text")
            planned = unit["planned"]
            if not isinstance(planned, dict) or set(planned) != {"unit_id", "expected_issue_alias"}:
                raise ValueError("Invalid reviewed plan")
            plan.append(PlannedUnit(UnitId(**planned["unit_id"]), planned["expected_issue_alias"]))
        plan = tuple(plan)
    except (KeyError, TypeError, ValueError) as error:
        raise PreviewError("email_preview_frozen_invalid") from error
    result = restore_public_result(frozen["report"], allowed_units=plan)
    if "presentation" in frozen:
        from .report_links import validate_foundry_link
        presentation = frozen["presentation"]
        if (
            not isinstance(presentation, dict) or set(presentation) - {"report_access"} != {
                "assignments", "foundry_links", "scoring_link", "blockers", "private_report_artifact",
            }
            or not isinstance(presentation["assignments"], dict)
            or not isinstance(presentation["foundry_links"], dict)
            or not isinstance(presentation["blockers"], list)
            or presentation["private_report_artifact"] != "presentation/report"
        ):
            raise PreviewError("email_preview_frozen_invalid")
        if not set(presentation["foundry_links"]) <= {unit.unit_id.agent for unit in plan}:
            raise PreviewError("email_preview_frozen_invalid")
        for href in presentation["foundry_links"].values():
            validate_foundry_link(href)
        if presentation.get("report_access") != request.report_access:
            raise PreviewError("email_preview_delivery_mismatch")
    mode = "test" if request.test_run else "official" if result.team_report_eligible else "failure"
    official_recipient = (
        request.delivery_binding["to_address"] if request.delivery_binding else TEAM_RECIPIENT
    )
    if request.mode != mode or request.recipient != (
        official_recipient if mode == "official" else frozen["recipient"]
    ):
        raise PreviewError("email_preview_delivery_mismatch")
    # Bind the frozen plan and source to this run, not a later result or reused lane.
    run = records.read_completed("run")
    if (
        run.get("kind") != "daily" or run.get("report_date") != request.report_date
        or run.get("test_run") is not request.test_run
        or type(run.get("rerun")) is not int or run["rerun"] != request.rerun
        or run.get("source_revision") != metadata.source_revision
        or run.get("targets") != [
            f"{unit.unit_id.agent}/{unit.unit_id.logical_version}" for unit in plan
        ]
    ):
        raise PreviewError("email_preview_delivery_mismatch")
    return frozen, plan, result, metadata


def _work_item_presentation(runtime, request, frozen):
    """Use a retained legacy snapshot only when its saved text binds this email."""
    from .work_items import unavailable_context

    private_context = frozen["private_context"]
    if "work_item_context" in frozen:
        return frozen["work_item_context"], private_context, {"source": "delivery_inputs"}
    retained = runtime.run(request.delivery_id).read_completed("work-item-context", missing_ok=True)
    if retained is None:
        return unavailable_context("work_item_legacy_snapshot"), private_context, {
            "source": "legacy_snapshot_unavailable",
        }
    from .work_items import legacy_work_item_email_context

    context, remaining_context = legacy_work_item_email_context(retained, private_context)
    provenance = {
        "source": "same_run_retained_checkpoint",
        "checkpoint_sha256": sha256(_encode(retained)).hexdigest(),
        "retained_text_prefix_removed": False,
    }
    if context["status"] == "unavailable":
        return context, private_context, provenance
    saved_text = retained["text"]
    if (
        context["snapshot"]["report_date"] != request.report_date or private_context is None
        or not private_context.startswith(saved_text)
    ):
        raise PreviewError("email_preview_work_item_context_mismatch")
    remainder = private_context[len(saved_text):]
    if remainder and not remainder.startswith("\n\n"):
        raise PreviewError("email_preview_work_item_context_mismatch")
    provenance.update(source="same_run_legacy_snapshot", retained_text_prefix_removed=True)
    # Do not strip, parse, or globally replace notes after the exact saved prefix.
    return context, remaining_context, provenance


def _renderer_provenance(root: Path) -> dict:
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=root,
        capture_output=True, text=True, check=False,
    )
    if revision.returncode or re.fullmatch(r"[0-9a-f]{40}", revision.stdout.strip()) is None:
        raise PreviewError("email_preview_renderer_revision_unavailable")
    # HEAD alone does not identify a local presentation edit under review.
    modules = {
        name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in (
            "email_preview.py", "email.py", "reporting.py", "report_context.py",
            "privacy.py", "results.py", "scoring.py", "work_items.py",
            "report_review.py", "report_links.py",
            "report_access.py",
        )
    }
    return {
        "source_revision": revision.stdout.strip(), "module_sha256": modules,
        "content_sha256": sha256(_encode(modules)).hexdigest(),
    }


def _banner(
    html: str, *, delivery_id: str, attachment: bool = False, scoring_derivation: dict | None = None,
) -> str:
    label = "LOCAL SCORING PREVIEW" if scoring_derivation else "LOCAL PRESENTATION PREVIEW"
    scoring_notice = ""
    if scoring_derivation:
        scoring_notice = (
            "<br>Scoring policy changed; counts, judgments and coverage are unchanged. "
            f"Score: {escape(str(scoring_derivation['source_score']))} &rarr; "
            f"{escape(str(scoring_derivation['derived_score']))}."
        )
    notice = (
        '<div role="note" style="box-sizing:border-box;max-width:960px;margin:16px auto 0;'
        'padding:14px 20px;background:#fff5e4;color:#704c16;'
        'font:14px/21px Segoe UI,Arial,sans-serif;border-left:4px solid #c47f15;'
        'overflow-wrap:anywhere;">'
        f"<strong>{label} &mdash; NOT SENT</strong><br>"
        f"Delivery: {escape(delivery_id)}. Same frozen measurement; no remeasurement.<br>"
        "The prepared email is unchanged. Measurement and renderer provenance are in manifest.json."
        + scoring_notice
        + ("<br>Open the attached report.md; its per-Agent headings contain human validation."
           if attachment else "")
        + "</div>"
    )
    match = re.search(r"<body(?:\s[^>]*)?>", html, re.I)
    if match is None:
        raise PreviewError("email_preview_html_invalid")
    return html[:match.end()] + notice + html[match.end():]


def _mime(
    request: EmailRequest, subject: str, html: str, report: str | None, *,
    restyle: bool, rescore: bool = False,
) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    message["To"] = request.recipient
    message["Subject"] = subject
    message["X-Unsent"] = "1"
    message["X-AIQ-Local-Preview"] = (
        "scoring-rescore" if rescore else "presentation-restyle" if restyle else "exact-prepared-request"
    )
    # Byte content avoids the newline normalization in set_content(str), so the
    # decoded exact-export body remains identical to the prepared HTML.
    message.set_content(html.encode("utf-8"), maintype="text", subtype="html", cte="base64")
    message.set_param("charset", "utf-8")
    if report is not None:
        message.add_attachment(
            report.encode("utf-8"), maintype="text", subtype="markdown",
            cte="base64", filename="report.md",
        )
        message.get_payload()[-1].set_param("charset", "utf-8")
        message.set_boundary("aiq-preview-" + sha256((html + report).encode("utf-8")).hexdigest())
    return message.as_bytes()


def _save_export(runtime, directory, files, manifest):
    # Reuse the checkpoint primitives: ownership, path checks, durable byte
    # writes, and exact replay. The manifest is the last committed file.
    contents = {**files, "manifest.json": _encode(manifest)}
    with runtime._write_lock:
        if not runtime._owned:
            raise StateError("state_not_owned")
        existing = {}
        for name, content in contents.items():
            path = _inside(runtime.root, directory / name)
            try:
                with _open_snapshot(path) as stream:
                    existing[name] = stream.read()
            except FileNotFoundError:
                continue
            if existing[name] != content:
                raise StateConflict()
        # Preflight the entire bundle before repairing a partial export.
        for name, content in contents.items():
            path = _inside(runtime.root, directory / name)
            if name in existing:
                _confirm_durable(path)
            else:
                _atomic_write(path, content)


def export_email_preview(
    runtime: RuntimeStore, delivery_id: str, *, root: Path, restyle: bool = False,
    scoring_revision: str | None = None, rescore: bool = False,
) -> EmailPreview:
    """Export only beneath Daily/previews; no arbitrary output or recipient."""
    if runtime.environment != "daily":
        raise PreviewError("email_preview_daily_only")
    if type(restyle) is not bool or type(rescore) is not bool or len(_parts(delivery_id)) != 1:
        raise PreviewError("email_preview_identity_invalid")
    if rescore and not restyle:
        raise PreviewError("email_preview_rescore_requires_restyle")
    if not runtime._owned:
        raise StateError("state_not_owned")
    record = read_email(runtime.outbox("email"), delivery_id)
    request = record.request
    restored = _frozen_inputs(runtime, request)
    if restyle and restored is None:
        raise PreviewError("email_preview_frozen_inputs_missing")
    renderer = _renderer_provenance(root)
    html, subject = request.html, request.subject
    markdown = (
        "# Detailed report unavailable\n\n"
        "This legacy delivery has no frozen result/context. Its prepared email is exported "
        "unchanged; no measurement or detailed report has been reconstructed.\n"
    )
    eml_html = html
    detail_kind = "frozen_inputs_unavailable_notice"
    context_matches = None
    work_item_provenance = None
    metadata = None
    blockers, agent_links, scoring_link = [], {}, None
    review = None
    assignments = None
    scoring_derivation = None
    derived_result = None
    if scoring_revision is not None:
        scoring_link = VerifiedScoringLink(root, scoring_revision)
    if restored is not None:
        frozen, plan, result, metadata = restored
        from .scoring import SCORING_POLICY

        if restyle and not rescore and result.scoring_policy != SCORING_POLICY and scoring_link is not None:
            raise PreviewError("email_preview_scoring_link_policy_mismatch")
        if rescore:
            from .results import rescore_result

            original = result.to_dict()
            result = rescore_result(result)
            derived_result = result.to_dict()
            scoring_derivation = {
                "source_policy": original["scoring_policy"],
                "derived_policy": derived_result["scoring_policy"],
                "source_score": original["score"], "derived_score": derived_result["score"],
                "source_result_sha256": sha256(_encode(original)).hexdigest(),
                "derived_result_sha256": sha256(_encode(derived_result)).hexdigest(),
                "classifications_changed": False, "coverage_changed": False,
            }
        if request.report_access is not None and not rescore:
            from .report_access import read_report_access
            access = read_report_access(runtime, request.report_access, delivery_id=delivery_id)
            if access.expired():
                blockers.append("report_access_expired_needs_explicit_new_revision")
        current = None
        if restyle:
            current = load_report_context(root, allowed_units=plan)
            context_matches = current.to_private_dict()["units"] == frozen["report_context"]["units"]
            if not context_matches:
                raise PreviewError("email_preview_reviewed_context_changed")
            agent_links, missing_links = foundry_links(runtime, delivery_id, plan)
            blockers.extend(missing_links)
            if scoring_link is None:
                retained = None if rescore else frozen.get("presentation", {}).get("scoring_link")
                if retained:
                    scoring_link = VerifiedScoringLink.from_retained(root, retained)
                elif result.scoring_policy == SCORING_POLICY:
                    try:
                        scoring_link = configured_scoring_link(runtime, root)
                    except (QualityError, OSError):
                        blockers.append("scoring_link_verification_failed")
            if scoring_link is None:
                blockers.append("scoring_link_publication_required")
            assignments = {
                "source": "current_reviewed_catalog_presentation_only",
                "catalog_sha256": sha256((root / "catalogs" / "AGENT_CATALOG.yaml").read_bytes()).hexdigest(),
                "owners": current.assignments,
            }
        # Exact exports need no current catalog, even if the old unit no longer
        # exists. Their optional attachment shows frozen counts without new prose.
        markdown = render_markdown(
            result, allowed_units=plan, warnings=tuple(frozen["warnings"]),
            report_context=current, metadata=metadata, delivery_id=delivery_id,
        )
        detail_kind = "frozen_result"
        if not restyle and "presentation" in frozen:
            retained_report = runtime.run(delivery_id).read_artifact("presentation/report")
            if (
                retained_report.get("format") != "markdown" or retained_report.get("private") is not True
                or not isinstance(retained_report.get("markdown"), str)
            ):
                raise PreviewError("email_preview_frozen_invalid")
            markdown = retained_report["markdown"]
            detail_kind = "frozen_private_markdown"
        if restyle:
            review = RetainedReviewContext(runtime, delivery_id, result)
            if "presentation" in frozen:
                retained_report = runtime.run(delivery_id).read_artifact("presentation/report")
                if retained_report.get("retained_review") != review.provenance():
                    raise PreviewError("email_preview_retained_review_changed")
            markdown = render_private_markdown(
                result, allowed_units=plan, warnings=tuple(frozen["warnings"]),
                report_context=current, metadata=metadata, delivery_id=delivery_id,
                review_context=review,
            )
            from .email import render_email_content
            work_items, private_context, work_item_provenance = _work_item_presentation(
                runtime, request, frozen,
            )
            kwargs = dict(
                allowed_units=plan, report_date=request.report_date, test_run=request.test_run,
                private_context=private_context, warnings=tuple(frozen["warnings"]),
                work_item_context=work_items, report_context=current,
                region_display=metadata.region_display, source_revision=metadata.source_revision,
                delivery_id=delivery_id,
                scoring_link=scoring_link, agent_links=agent_links,
            )
            subject, html = render_email_content(result, details_href="report.html", **kwargs)
            eml_subject, eml_html = render_email_content(
                result, details_href=None, attached_report=True, **kwargs,
            )
            if subject != eml_subject:
                raise PreviewError("email_preview_subject_mismatch")
            label = "LOCAL SCORING PREVIEW" if rescore else "LOCAL PRESENTATION PREVIEW"
            subject = f"[{label}] " + subject
            html = _banner(html, delivery_id=delivery_id, scoring_derivation=scoring_derivation)
            eml_html = _banner(
                eml_html, delivery_id=delivery_id, attachment=True, scoring_derivation=scoring_derivation,
            )
            if rescore:
                detail_kind = "rescored_frozen_private_markdown"
                markdown = (
                    "# Local scoring preview - not sent\n\n"
                    "Derived with a new scoring policy from unchanged saved counts, judgments and coverage. "
                    "The original result and prepared email are unchanged.\n\n"
                    f"Scoring policy: {result.scoring_policy.version}.\n\n" + markdown
                )
            else:
                markdown = (
                    "# Local presentation preview - not sent\n\n"
                    "The original prepared request and frozen measurement are unchanged.\n\n" + markdown
                )
    report = markdown_view(markdown)
    _validate_html(html, local_links=True)
    _validate_html(report, local_links=False)
    # An exact export never rewrites even legacy link text. Restyled MIME has
    # no relative/file/cid links; its details are a real attachment instead.
    _validate_html(eml_html, local_links=not restyle)
    files = {
        "email.html": html.encode("utf-8"),
        "email.eml": _mime(
            request, subject, eml_html, markdown, restyle=restyle, rescore=rescore,
        ),
        "report.md": markdown.encode("utf-8"),
        "report.html": report.encode("utf-8"),
    }
    if derived_result is not None:
        files["result.json"] = _encode(derived_result)
    manifest = {
        "schema_version": "1.0", "delivery_id": delivery_id,
        "export_kind": (
            "local_scoring_preview" if rescore
            else "local_presentation_preview" if restyle else "exact_prepared_request"
        ),
        "local_only": True, "send_authorized": False, "measurement_changed": False,
        "prepared_request_sha256": sha256(_encode(request.to_private_dict())).hexdigest(),
        "prepared_status_observed": record.status,
        "recipient": request.recipient, "subject": subject, "prepared_subject": request.subject,
        "mode": request.mode, "test_run": request.test_run, "rerun": request.rerun,
        "report_date": request.report_date, "report_kind": detail_kind,
        "measurement_source_revision": metadata.source_revision if metadata else None,
        "renderer": renderer, "reviewed_units_match": context_matches,
        "work_item_presentation": work_item_provenance,
        "assignment_presentation": assignments,
        "links": {"scoring": scoring_link.to_dict() if scoring_link else None,
                  "foundry": agent_links},
        "blockers": blockers,
        "retained_review": review.provenance() if review else None,
        "authoritative_report": "report.md",
        "report_html_derived_from": "report.md",
        "eml_attachments": ["report.md"],
        "frozen_inputs_sha256": sha256(_encode(restored[0])).hexdigest() if restored else None,
        "files": {name: {"sha256": sha256(content).hexdigest(), "bytes": len(content)}
                  for name, content in files.items()},
    }
    if scoring_derivation is not None:
        manifest["scoring_derivation"] = scoring_derivation
    presentation_id = sha256(_encode(manifest)).hexdigest()
    manifest["presentation_id"] = presentation_id
    directory = _inside(runtime.root, runtime.directory / "previews" / delivery_id / presentation_id)
    _save_export(runtime, directory, files, manifest)
    return EmailPreview(delivery_id, presentation_id, directory, restyle, tuple(blockers), rescore)
