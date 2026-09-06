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
from .publication import AzureCliAdxClient, PublicationOutbox, build_public_report
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


def _generated_paths(report_date: str) -> tuple[str, ...]:
    day = date.fromisoformat(report_date)
    prefix = day.strftime("reports/daily/%Y/%m/%d")
    return (prefix + "/report.json", prefix + "/report.md", "reports/latest.json", "reports/latest.md")


def _write_report(root: Path, document: Mapping, *, test_run: bool) -> tuple[str, ...]:
    from .public_artifacts import write_public_report
    for name in _generated_paths(document["report_date"]):
        path = root.joinpath(*name.split("/"))
        if path.absolute() != path.resolve() or not path.resolve().is_relative_to(root.resolve()):
            raise QualityError("public_report_path_invalid")
    return write_public_report(root, document, test_run=test_run)


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
        write_report: Callable = _write_report,
    ) -> None:
        self.root, self.runtime, self.run_id = root, runtime, run_id
        self.records = runtime.run(run_id)
        self.allowed_units, self.report_date, self.test_run = allowed_units, report_date, test_run
        self.fetch_context, self.adx_factory, self.outbox_factory = fetch_context, adx_factory, outbox_factory
        self.tick, self.write_report = tick, write_report
        self.warnings: set[str] = set()
        self.context: str | None = None
        self.work_item_context: dict | None = None
        self.logger: RunLogger | None = None
        self.outbox = self.client = self.worker = None
        self.stop = asyncio.Event()
        self.disabled = False

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
        if self.test_run or self.runtime.environment != "daily" or not result.team_report_eligible:
            return {}
        output = {}
        metadata = {
            "report_date": self.report_date.isoformat(),
            "source_commit": source_revision, "region": environment.region_display,
        }
        try:
            document = build_public_report(
                result, allowed_units=self.allowed_units, framework_run_id=self.run_id, **metadata,
            )
            self.records.save_artifact("publication/report", document)
            output["public_report_path"] = str(self.records._path("artifacts", "publication/report"))
        except (QualityError, OSError):
            self.warn("github_publication_failed")
            self.warn("adx_delivery_failed")
            return output
        if self.outbox is not None and not self.disabled:
            try:
                self.outbox.queue_report(result, **metadata)
            except (QualityError, OSError):
                self.warn("adx_delivery_failed")
        try:
            expected = _generated_paths(metadata["report_date"])
            paths = self.write_report(self.root, document, test_run=False)
            if tuple(paths) != expected:
                raise QualityError("public_report_paths_mismatch")
            request = {
                "schema_version": "1.0", "operation": "publish-generated-report",
                "repository": "ninghu/agent-insights-quality", "base_branch": "main",
                "branch": "generated/quality-" + metadata["report_date"],
                "title": "Agent Insights quality - " + metadata["report_date"],
                "body": "Publish the generated daily quality report. Changes are restricted to the listed report files.",
                "allowed_paths": list(expected), "source_commit": source_revision,
                "report_date": metadata["report_date"],
            }
            self.records.save_artifact("publication/github-request", request)
            output.update(
                generated_paths=list(expected),
                github_request_path=str(self.records._path("artifacts", "publication/github-request")),
            )
        except (QualityError, OSError):
            self.warn("github_publication_failed")
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
            frozen = {
                "report": result.to_dict(), "test_run": self.test_run, "rerun": rerun,
                "report_date": self.report_date.isoformat(), "region_display": environment.region_display,
                "source_revision": source_revision, "private_context": private_context,
                "work_item_context": deepcopy(self.work_item_context) if self.work_item_context is not None
                else unavailable_context("work_item_context_missing"),
                "warnings": sorted(warnings), "recipient": recipient(),
                "report_context": context.to_private_dict(),
                "configured_assessor": configured,
            }
            self.records.save_completed("delivery-inputs", frozen)
        elif frozen["report_context"] != context.to_private_dict():
            raise QualityError("delivery_reviewed_context_changed")
        result = restore_public_result(frozen["report"], allowed_units=self.allowed_units)
        prepare_email(
            outbox, self.run_id, result, allowed_units=self.allowed_units,
            report_date=frozen["report_date"], test_run=frozen["test_run"], rerun=frozen["rerun"],
            test_recipient=frozen["recipient"], failure_recipient=frozen["recipient"],
            private_context=frozen["private_context"], warnings=tuple(frozen["warnings"]),
            work_item_context=frozen.get("work_item_context", unavailable_context("work_item_legacy_snapshot")),
            region_display=frozen["region_display"], source_revision=frozen["source_revision"],
            report_context=context,
        )
        return read_email(outbox, self.run_id)
