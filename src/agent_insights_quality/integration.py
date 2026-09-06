"""Optional CLI integrations, isolated from qualification and app-native sending."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import uuid

from .contracts import Environment
from .email import prepare_email, read_email
from .errors import QualityError
from .events import RunLogger
from .privacy import restore_public_result
from .publication import AzureCliAdxClient, PublicationOutbox
from .private_publication import AzurePrivateReportBlob, PrivateReportOutbox
from .results import PlannedUnit, QualityResult
from .settings import AssessmentSettings
from .state import RuntimeStore, StateError
from .work_items import (
    fetch_quality_context, unavailable_context, validate_work_item_context,
)


def private_path(runtime: RuntimeStore, path: Path) -> Path:
    path = path.absolute()
    if path.resolve() != path or not path.is_relative_to(runtime.root):
        raise QualityError("private_path_invalid")
    return path


def read_object(path: Path) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate field")
            value[key] = item
        return value
    try:
        with path.open("rb") as stream:
            content = stream.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError("Oversized input")
        value = json.loads(content, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("Object required")
        return value
    except (OSError, ValueError, UnicodeError) as error:
        raise QualityError("private_input_invalid") from error


def _exception_diagnostics(error: BaseException) -> dict:
    """Bounded private structure only; never format exception messages or locals."""
    exceptions, seen = [], set()
    current, relation = error, "raised"
    while current is not None and id(current) not in seen and len(exceptions) < 8:
        seen.add(id(current))
        frames = deque(maxlen=32)
        trace, traversed = current.__traceback__, 0
        while trace is not None and traversed < 256:
            code = trace.tb_frame.f_code
            frames.append({
                "filename": code.co_filename[:1024], "lineno": trace.tb_lineno,
                "function": code.co_name[:256],
            })
            trace, traversed = trace.tb_next, traversed + 1
        kind = type(current)
        item = {
            "exception_type": f"{kind.__module__}.{kind.__qualname__}"[:256],
            "relation": relation, "frames": list(frames),
            "frames_truncated": traversed > 32 or trace is not None,
        }
        for name in ("errno", "winerror"):
            value = getattr(current, name, None)
            item[name] = value if type(value) is int and -(2**63) <= value < 2**63 else None
        exceptions.append(item)
        if current.__cause__ is not None:
            current, relation = current.__cause__, "cause"
        elif current.__context__ is not None:
            relation = "suppressed_context" if current.__suppress_context__ else "context"
            current = current.__context__
        else:
            current = None
    return {
        "schema_version": "1.0", "exceptions": exceptions,
        "chain_truncated": current is not None,
    }


@contextmanager
def command_status(runtime: RuntimeStore, command: str):
    """Persist safe startup failures too; programming exceptions still propagate."""
    records = runtime.run("startup-" + uuid.uuid4().hex)
    logger = RunLogger(records.directory, test_run=True)
    try:
        records.save_progress("command-status", {"command": command, "status": "started", "code": "ok"})
        logger.emit("started")
        yield records
    except BaseException as error:
        code = error.code if isinstance(error, QualityError) else (
            "command_io_failed" if isinstance(error, OSError) else "unexpected_failure"
        )
        logger.emit("failure", code=code)
        try:
            diagnostics = "diagnostics/exception"
            records.save_artifact(diagnostics, _exception_diagnostics(error))
            value = {"command": command, "status": "blocked", "code": code}
            value["diagnostics_path"] = str(records._path("artifacts", diagnostics))
            if isinstance(error, QualityError):
                value.update(request_accepted=error.request_accepted, retryable=error.retryable)
            records.save_progress("command-status", value)
        except StateError:
            logger.emit("failure", code="state_checkpoint_failed")
            raise
        raise
    else:
        records.save_progress("command-status", {"command": command, "status": "completed", "code": "ok"})
        logger.emit("completed")
    finally:
        logger.close()


async def _tick(stop: asyncio.Event) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=15)
        return True
    except TimeoutError:
        return False


async def _context(
    query_url: str, report_date: date, *, previous_snapshot: dict | None = None,
) -> dict:
    from .providers import AzureHttpTransport
    return await fetch_quality_context(
        query_url, report_date, AzureHttpTransport(), previous_snapshot=previous_snapshot,
    )


def _previous_official_snapshot(runtime: RuntimeStore, run_id: str) -> dict | None:
    """Read only durable email handoffs and their immutable delivery inputs.

    Accepted means successfully submitted, not confirmed inbox delivery. The
    latest acknowledged snapshot cutoff is the reporting high-water mark; file
    timestamps, report-date midnights, TEST and uncertain sends are never anchors.
    No history is rewritten and no raw run evidence is inspected.
    """
    outbox = runtime.outbox("email")
    identifiers = set()
    for collection in ("completed", "progress"):
        directory = private_path(runtime, outbox.directory / collection)
        for path in directory.glob("*.json"):
            identifiers.add(path.stem)
            if len(identifiers) > 10_000:
                raise QualityError("work_item_anchor_history_limit")
    latest = None
    for identifier in sorted(identifiers - {run_id}):
        try:
            record = read_email(outbox, identifier)
        except QualityError as error:
            raise QualityError("work_item_anchor_metadata_invalid") from error
        if (
            record.status not in {"accepted", "delivered"}
            or record.request.mode != "official" or record.request.test_run
        ):
            continue
        try:
            frozen = runtime.run(identifier).read_completed("delivery-inputs", missing_ok=True)
        except StateError as error:
            raise QualityError("work_item_anchor_metadata_invalid") from error
        if frozen is None:
            raise QualityError("work_item_anchor_metadata_missing")
        if (
            frozen.get("test_run") is not False or type(frozen.get("rerun")) is not int
            or frozen["rerun"] != 0 or frozen.get("report_date") != record.request.report_date
            or not isinstance(frozen.get("report"), dict)
            or frozen["report"].get("team_report_eligible") is not True
        ):
            raise QualityError("work_item_anchor_metadata_invalid")
        if "work_item_context" not in frozen:
            raise QualityError("work_item_anchor_legacy")
        try:
            context = validate_work_item_context(frozen["work_item_context"])
        except QualityError as error:
            raise QualityError("work_item_anchor_metadata_invalid") from error
        if context["status"] == "unavailable":
            continue
        if context["schema_version"] != "1.0":
            raise QualityError("work_item_anchor_legacy")
        snapshot = context["snapshot"]
        if snapshot["report_date"] != record.request.report_date:
            raise QualityError("work_item_anchor_metadata_invalid")
        cutoff = snapshot["window"]["end"]
        if latest is None or cutoff > latest["cutoff"]:
            latest = {"delivery_id": identifier, "cutoff": cutoff}
    return latest


def _retained_context(value: dict) -> tuple[dict, str | None]:
    """Read legacy text without inventing Type/window fields or replacing it."""
    if "schema_version" in value:
        return deepcopy(validate_work_item_context(value)), None
    if not isinstance(value.get("status"), str) or value["status"] not in {"available", "unavailable"}:
        raise StateError("work_item_checkpoint_invalid")
    if value["status"] == "unavailable":
        if set(value) != {"status", "code", "text"} or value["text"] is not None:
            raise StateError("work_item_checkpoint_invalid")
        return unavailable_context(value["code"]), None
    if (
        set(value) != {"status", "snapshot", "text"} or not isinstance(value["snapshot"], dict)
        or not isinstance(value["text"], str)
    ):
        raise StateError("work_item_checkpoint_invalid")
    return unavailable_context("work_item_legacy_snapshot"), value["text"]


class RunIntegration:
    """One run's bounded outbox worker; explicit test mode never touches ADX.

    Hold RuntimeStore.ownership through __aexit__: PublicationOutbox drains any
    in-flight thread before cancellation releases ownership or closes the client.
    """

    def __init__(
        self, root: Path, runtime: RuntimeStore, run_id: str, *,
        allowed_units: tuple[PlannedUnit, ...], report_date: date, test_run: bool,
        fetch_context: Callable[..., Awaitable[dict]] = _context,
        adx_factory=AzureCliAdxClient, outbox_factory=PublicationOutbox,
        tick: Callable[[asyncio.Event], Awaitable[bool]] = _tick,
        private_blob_factory=None,
    ) -> None:
        self.root, self.runtime, self.run_id = root, runtime, run_id
        self.records = runtime.run(run_id)
        self.allowed_units, self.report_date, self.test_run = allowed_units, report_date, test_run
        self.fetch_context, self.adx_factory, self.outbox_factory = fetch_context, adx_factory, outbox_factory
        self.tick, self.private_blob_factory = tick, private_blob_factory or AzurePrivateReportBlob
        self.warnings: set[str] = set()
        self.context: str | None = None
        self.work_item_context: dict | None = None
        self.logger: RunLogger | None = None
        self.outbox = self.client = self.worker = None
        self.stop = asyncio.Event()
        self.disabled = False
        self.private_disabled = False

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.add(code)
            if self.logger:
                self.logger.emit("warning", stage="outbox", code=code)

    def attach_logger(self, logger: RunLogger) -> None:
        self.logger = logger
        for code in sorted(self.warnings):
            logger.emit("warning", stage="outbox", code=code)

    async def _freeze_context(self) -> None:
        if self.runtime.environment != "daily":
            return
        frozen = self.records.read_completed("delivery-inputs", missing_ok=True)
        if frozen is not None:
            self.context = frozen["private_context"]
            self.work_item_context = (
                validate_work_item_context(frozen["work_item_context"])
                if "work_item_context" in frozen else unavailable_context("work_item_legacy_snapshot")
            )
            if "work_item_unavailable" in frozen["warnings"]:
                self.warn("work_item_unavailable")
            return
        value = self.records.read_completed("work-item-context", missing_ok=True)
        if value is None:
            pending = self.records.read("work-item-context", missing_ok=True)
            if pending:
                value = unavailable_context("work_item_context_interrupted")
            else:
                # A partial fetch is not repeated under a different email snapshot.
                self.records.save_progress("work-item-context", {"status": "fetching"})
                try:
                    previous = _previous_official_snapshot(self.runtime, self.run_id)
                    path = private_path(
                        self.runtime, self.runtime.root / "config" / "quality-work-items-query-url.txt",
                    )
                    with path.open("r", encoding="utf-8") as stream:
                        query = stream.read(8193)
                    if not query.strip() or len(query) > 8192:
                        raise QualityError("work_item_query_invalid")
                    snapshot = await asyncio.wait_for(
                        self.fetch_context(
                            query.strip(), self.report_date, previous_snapshot=previous,
                        ), timeout=60,
                    )
                    value = validate_work_item_context({
                        "schema_version": "1.0", "status": "available", "snapshot": snapshot,
                    })
                    if snapshot["report_date"] != self.report_date.isoformat():
                        raise QualityError("work_item_snapshot_identity_invalid")
                    window = snapshot["window"]
                    if (
                        snapshot["query_url"] != query.strip()
                        or previous is None and window["basis"] != "initial_lookback"
                        or previous is not None and (
                            window["basis"] != "previous_official_report"
                            or window["start"] != previous["cutoff"]
                            or window["previous_delivery_id"] != previous["delivery_id"]
                        )
                    ):
                        raise QualityError("work_item_snapshot_identity_invalid")
                except (QualityError, OSError, UnicodeError) as error:
                    value = unavailable_context(
                        error.code if isinstance(error, QualityError) else "work_item_unavailable",
                    )
            self.records.save_completed("work-item-context", value)
        self.work_item_context, self.context = _retained_context(value)
        if self.work_item_context["status"] == "unavailable":
            self.warn("work_item_unavailable")

    async def __aenter__(self):
        await self._freeze_context()
        if self.test_run:
            return self
        try:
            self.outbox = self.outbox_factory(
                self.runtime.outbox("publication"), framework_run_id=self.run_id,
                profile=self.runtime.environment, allowed_units=self.allowed_units,
            )
            path = private_path(self.runtime, self.runtime.root / "config" / "adx.json")
            config = read_object(path)
            if (
                set(config) != {"schema_version", "cluster_uri", "database"}
                or config["schema_version"] != "1.0"
                or not isinstance(config["cluster_uri"], str)
                or not isinstance(config["database"], str)
            ):
                raise QualityError("publication_adx_config_invalid")
            self.client = self.adx_factory(config["cluster_uri"], config["database"])
        except (QualityError, OSError):
            self.warn("adx_delivery_failed")
        if self.client is not None:
            self.worker = asyncio.create_task(self._publish_loop())
        return self

    def queue_event(self, event: Mapping) -> None:
        if self.test_run or self.outbox is None or self.disabled:
            return
        try:
            self.outbox.queue_event(event)
        except (QualityError, OSError):
            self.disabled = True
            self.warn("adx_delivery_failed")

    async def _flush(self) -> None:
        if self.client is None or self.disabled:
            return
        try:
            result = await self.outbox.flush_async(self.client, batch_size=100, max_batches=2)
            if result.warnings:
                self.warn("adx_delivery_failed")
        except (QualityError, OSError):
            # A broken optional outbox stops only that sink, never its producer.
            self.disabled = True
            self.warn("adx_delivery_failed")

    async def _publish_loop(self) -> None:
        while not await self.tick(self.stop):
            await self._flush()
            if self.disabled:
                return
        await self._flush()

    async def finish_publication(self) -> None:
        self.stop.set()
        try:
            if self.worker is not None:
                try:
                    await self.worker
                finally:
                    self.worker = None
        finally:
            if self.client is not None:
                try:
                    self.client.close()
                except (QualityError, OSError):
                    self.warn("adx_delivery_failed")
                finally:
                    self.client = None

    async def __aexit__(self, *_):
        await self.finish_publication()

    def publish_report(self, result: QualityResult, environment: Environment, source_revision: str) -> dict:
        if self.runtime.environment != "daily":
            return {}
        output = {}
        metadata = {
            "report_date": self.report_date.isoformat(),
            "source_commit": source_revision, "region": environment.region_display,
        }
        if not self.test_run and result.team_report_eligible and self.outbox is not None and not self.disabled:
            try:
                self.outbox.queue_report(result, **metadata)
            except (QualityError, OSError):
                self.warn("adx_delivery_failed")
        if self.private_disabled:
            return output
        logger = self.logger or RunLogger(self.records.directory, test_run=self.test_run)
        client = None
        try:
            private = PrivateReportOutbox(self.runtime, self.run_id)
            if (
                private.records.read_completed(private.request_key, missing_ok=True) is None
                and self.runtime.outbox("email").read(self.run_id, missing_ok=True) is not None
            ):
                # No implicit backfill/restyle of already prepared historical mail.
                return {"private_report": {"status": "not_prepared", "code": "existing_email_unchanged"}}
            logger.emit("started", stage="outbox", code="private_report_publication")
            request = private.prepare(
                self.root, result, allowed_units=self.allowed_units, environment=environment,
                source_revision=source_revision, report_date=self.report_date.isoformat(),
                test_run=self.test_run,
            )
            output["private_report"] = private.status(request)
            if output["private_report"]["status"] not in {"delivered", "conflict"}:
                client = self.private_blob_factory(request["account"])
                output["private_report"] = private.flush(client)
            if output["private_report"]["status"] != "delivered":
                self.warn("private_report_publication_failed")
                logger.emit("warning", stage="outbox", code="private_report_publication_failed")
                code = output["private_report"].get("code", "private_report_pending")
                logger.emit("warning", stage="outbox", code=code)
            else:
                logger.emit("completed", stage="outbox", code="private_report_delivered")
            if output["private_report"].get("receipt_path"):
                from .report_access import VerifiedReportAccess, prepare_report_access
                access_key = self.run_id + "/" + request["presentation_id"] + "/access/initial"
                try:
                    access = None
                    if private.records.read_completed(access_key, missing_ok=True) is not None:
                        access = VerifiedReportAccess(self.runtime, access_key)
                    elif self.runtime.outbox("email").read(self.run_id, missing_ok=True) is None:
                        client = client or self.private_blob_factory(request["account"])
                        access = prepare_report_access(private, client)
                    if access:
                        output["private_report"]["access"] = access.status()
                        if access.expired():
                            self.warn("private_report_access_unavailable")
                except (QualityError, OSError) as error:
                    if isinstance(error, StateError):
                        self.private_disabled = True
                        self.warn("private_report_checkpoint_failed")
                    code = error.code if isinstance(error, QualityError) else "report_access_unavailable"
                    output["private_report"]["access"] = {"status": "blocked", "code": code}
                    logger.emit("warning", stage="outbox", code=code)
                    self.warn("private_report_access_unavailable")
            self.records.save_progress("private-publication", output["private_report"])
        except (QualityError, OSError) as error:
            if isinstance(error, StateError):
                self.private_disabled = True
                self.warn("private_report_checkpoint_failed")
            self.warn("private_report_publication_failed")
            code = error.code if isinstance(error, QualityError) else "private_report_unavailable"
            logger.emit("warning", stage="outbox", code=code)
            output["private_report"] = {
                **output.get("private_report", {}), "status": "pending", "code": code,
            }
            if not self.private_disabled:
                try:
                    self.records.save_progress("private-publication", output["private_report"])
                except (QualityError, OSError):
                    self.private_disabled = True
                    self.warn("private_report_checkpoint_failed")
        finally:
            if client is not None:
                try:
                    client.close()
                except (QualityError, OSError):
                    self.warn("private_report_publication_failed")
                    logger.emit("warning", stage="outbox", code="private_report_close_failed")
            if logger.health_warnings:
                self.warn("logging_failed")
            if self.logger is None:
                logger.close()
        return output

    def frozen_result(self) -> QualityResult | None:
        value = self.records.read_completed("delivery-inputs", missing_ok=True)
        if value and (
            value.get("test_run") is not self.test_run
            or value.get("report_date") != self.report_date.isoformat()
        ):
            raise StateError("delivery_identity_mismatch")
        return restore_public_result(value["report"], allowed_units=self.allowed_units) if value else None

    def prepare_delivery(
        self, result: QualityResult, environment: Environment, source_revision: str, *,
        rerun: int, recipient: Callable[[], str], assessment_settings: AssessmentSettings | None = None,
    ):
        from .report_context import load_report_context
        from .report_links import VerifiedScoringLink, configured_scoring_link, foundry_links
        from .report_review import RetainedReviewContext
        from .reporting import render_private_markdown
        from .report_context import ReportMetadata

        outbox = self.runtime.outbox("email")
        existing = outbox.read(self.run_id, missing_ok=True)
        if existing is not None:
            return read_email(outbox, self.run_id)
        frozen = self.records.read_completed("delivery-inputs", missing_ok=True)
        context = load_report_context(self.root, allowed_units=self.allowed_units)
        if frozen is None:
            warnings = set(self.warnings)
            if self.logger and self.logger.health_warnings:
                warnings.add("logging_failed")
            private_context = self.context
            configured = assessment_settings.to_dict() if assessment_settings is not None else None
            if self.test_run and configured is not None:
                assessor_context = (
                    "Configured assessment (intent, not observed serving metadata)\n"
                    f"Deployment: {configured['deployment_name']}\n"
                    f"Model: {configured['model']}\n"
                    f"Configured model version: {configured['model_version']}\n"
                    f"Credential: {configured['credential']}"
                )
                private_context = "\n\n".join(
                    part for part in (private_context, assessor_context) if part
                )
            links, link_blockers = foundry_links(self.runtime, self.run_id, self.allowed_units)
            try:
                scoring = configured_scoring_link(self.runtime, self.root)
            except (QualityError, OSError):
                scoring = None
            blockers = [*link_blockers]
            if scoring is None:
                blockers.append("scoring_link_publication_required")
            detail = self.records.read_artifact("presentation/report", missing_ok=True)
            if detail is None:
                try:
                    publication = self.runtime.outbox("private-reports").read_completed(
                        "requests/" + self.run_id, missing_ok=True,
                    )
                    if publication is not None:
                        publication = PrivateReportOutbox(self.runtime, self.run_id).request()
                except (QualityError, OSError):
                    publication = None
                    warnings.add("private_report_publication_failed")
                if publication is not None:
                    # Reuse the exact published MD, not a render with newer warnings.
                    markdown = publication["files"]["report.md"]
                    provenance = publication["retained_review"]
                else:
                    review = RetainedReviewContext(self.runtime, self.run_id, result)
                    markdown = render_private_markdown(
                        result, allowed_units=self.allowed_units, review_context=review,
                        warnings=tuple(sorted(warnings)), report_context=context,
                        metadata=ReportMetadata(self.report_date.isoformat(), environment.region_display, source_revision),
                        delivery_id=self.run_id,
                    )
                    provenance = review.provenance()
                detail = {
                    "format": "markdown", "private": True, "markdown": markdown,
                    "retained_review": provenance,
                }
                self.records.save_artifact("presentation/report", detail)
            markdown = detail["markdown"]
            from .state import _atomic_write, _confirm_durable, _inside, _open_snapshot
            report_path = _inside(
                self.runtime.root, self.records._path("artifacts", "presentation/report").with_suffix(".md"),
            )
            encoded_report = markdown.encode("utf-8")
            with self.runtime._write_lock:
                if not self.runtime._owned:
                    raise StateError("state_not_owned")
                try:
                    with _open_snapshot(report_path) as stream:
                        existing_report = stream.read()
                except FileNotFoundError:
                    _atomic_write(report_path, encoded_report)
                else:
                    if existing_report != encoded_report:
                        raise StateError("delivery_private_report_conflict")
                    _confirm_durable(report_path)
            from .report_access import VerifiedReportAccess
            access = None
            try:
                published = self.runtime.outbox("private-reports").read_completed(
                    "requests/" + self.run_id, missing_ok=True,
                )
                if published:
                    access_key = self.run_id + "/" + published["presentation_id"] + "/access/initial"
                    if self.runtime.outbox("private-reports").read_completed(access_key, missing_ok=True):
                        access = VerifiedReportAccess(self.runtime, access_key)
                        if access.expired():
                            blockers.append("report_access_expired_needs_explicit_new_revision")
                            access = None
            except (QualityError, OSError):
                warnings.add("private_report_access_unavailable")
            if access is None:
                blockers.append("human_validation_link_unavailable")
            frozen = {
                "report": result.to_dict(), "test_run": self.test_run, "rerun": rerun,
                "report_date": self.report_date.isoformat(), "region_display": environment.region_display,
                "source_revision": source_revision, "private_context": private_context,
                "work_item_context": deepcopy(self.work_item_context) if self.work_item_context is not None
                else unavailable_context("work_item_context_missing"),
                "warnings": sorted(warnings), "recipient": recipient(),
                "report_context": context.to_private_dict(),
                "configured_assessor": configured,
                "presentation": {
                    "assignments": context.assignments, "foundry_links": links,
                    "scoring_link": scoring.to_dict() if scoring else None,
                    "blockers": blockers, "private_report_artifact": "presentation/report",
                    **({"report_access": dict(access.descriptor)} if access else {}),
                },
            }
            self.records.save_completed("delivery-inputs", frozen)
        elif frozen["report_context"] != context.to_private_dict():
            raise QualityError("delivery_reviewed_context_changed")
        result = restore_public_result(frozen["report"], allowed_units=self.allowed_units)
        presentation = frozen.get("presentation", {})
        if presentation and presentation["assignments"] != context.assignments:
            raise QualityError("delivery_presentation_assignments_changed")
        scoring = (
            VerifiedScoringLink.from_retained(self.root, presentation["scoring_link"])
            if presentation.get("scoring_link") else None
        )
        from .report_access import read_report_access
        access = (
            read_report_access(self.runtime, presentation["report_access"], delivery_id=self.run_id)
            if presentation.get("report_access") else None
        )
        prepare_email(
            outbox, self.run_id, result, allowed_units=self.allowed_units,
            report_date=frozen["report_date"], test_run=frozen["test_run"], rerun=frozen["rerun"],
            test_recipient=frozen["recipient"], failure_recipient=frozen["recipient"],
            private_context=frozen["private_context"], warnings=tuple(frozen["warnings"]),
            work_item_context=frozen.get("work_item_context", unavailable_context("work_item_legacy_snapshot")),
            region_display=frozen["region_display"], source_revision=frozen["source_revision"],
            report_context=context,
            scoring_link=scoring, agent_links=presentation.get("foundry_links"),
            report_access=access,
        )
        return read_email(outbox, self.run_id)
