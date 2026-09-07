"""Small private launch reservation for the unified Daily entry point.

An active pointer is saved before its immutable descriptor and before any provider
work. Only terminal email evidence advances it; process completion is irrelevant.
All operations run under the existing environment ownership lock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
import re

from .delivery_recipient import (
    _check_identity, _identity, _private_address, literal_address, read_private_recipient,
)
from .errors import QualityError
from .state import RuntimeStore, StateError, _inside

_FIELDS = {
    "schema_version", "run_id", "report_date", "source_revision",
    "report_mode", "to_address", "rerun",
}
_TEST_ID = re.compile(r"daily-(\d{4}-\d{2}-\d{2})-test-([1-9][0-9]*)")
_TERMINAL = {"accepted", "delivered", "rejected"}


def validate_input(report_mode: str, to_address: str) -> str:
    if type(report_mode) is not str or report_mode not in {"test", "official"}:
        raise QualityError("automation_report_mode_invalid")
    return _private_address(to_address) if report_mode == "test" else literal_address(to_address)


def validate_launch(value: dict) -> dict:
    if type(value) is not dict or set(value) != _FIELDS or (
        value["schema_version"] != "1.0.0"
        or type(value["rerun"]) is not int
        or type(value["source_revision"]) is not str
        or re.fullmatch(r"[0-9a-f]{40}", value["source_revision"]) is None
        or type(value["report_date"]) is not str
    ):
        raise StateError("automation_launch_invalid")
    validate_input(value["report_mode"], value["to_address"])
    try:
        day = date.fromisoformat(value["report_date"])
        if day.isoformat() != value["report_date"]:
            raise ValueError()
    except ValueError:
        raise StateError("automation_launch_invalid") from None
    test_run = value["report_mode"] == "test"
    if (not 1 <= value["rerun"] < 10**58 if test_run else value["rerun"] != 0) or (
        not test_run and day.weekday() >= 5
    ):
        raise StateError("automation_launch_invalid")
    expected = f"daily-{day.isoformat()}" + (f"-test-{value['rerun']}" if test_run else "")
    if value["run_id"] != expected or len(expected) > 80:
        raise StateError("automation_launch_invalid")
    return value


def _retained_context(runtime: RuntimeStore, launch: dict) -> None:
    """Cross-check already-present source, identity and routing checkpoints."""
    records = runtime.run(launch["run_id"])
    test_run = launch["report_mode"] == "test"
    identity = _identity(runtime, launch["run_id"], test_run)
    for key in ("run", "traffic-intent", "delivery-inputs"):
        value = records.read_completed(key, missing_ok=True)
        if value is None:
            continue
        if value.get("source_revision") != launch["source_revision"]:
            raise StateError("automation_source_binding_mismatch")
        if key != "traffic-intent":
            _check_identity(value, identity)
        if key == "run" and value.get("kind") != "daily":
            raise StateError("automation_launch_invalid")
        if key == "traffic-intent" and (
            value.get("fresh_traffic") is not test_run or value.get("reuse_run_id") is not None
        ):
            raise StateError("automation_traffic_binding_mismatch")
        if key == "delivery-inputs" and value.get("delivery_binding") != launch:
            raise StateError("automation_delivery_binding_mismatch")
    private = records.read_completed("delivery-recipient", missing_ok=True)
    if private is not None:
        recipient = read_private_recipient(runtime, launch["run_id"], test_run=test_run)
        if test_run and recipient != launch["to_address"]:
            raise StateError("automation_delivery_binding_mismatch")
        delivery = records.read_completed("delivery-inputs", missing_ok=True)
        if delivery is not None and delivery.get("recipient") != recipient:
            raise StateError("automation_delivery_binding_mismatch")


def read_launch(runtime: RuntimeStore, run_id: str) -> dict | None:
    value = runtime.outbox("automation").read_completed("launches/" + run_id, missing_ok=True)
    if value is not None:
        validate_launch(value)
        if runtime.environment != "daily" or value["run_id"] != run_id:
            raise StateError("automation_launch_invalid")
        _retained_context(runtime, value)
    return value


def _next_rerun(runtime: RuntimeStore) -> int:
    """Reserve above every retained run/outbox identity, including legacy trials."""
    numbers = [0]

    def consider(name):
        match = _TEST_ID.fullmatch(name)
        if match:
            if len(name) > 80:
                raise StateError("automation_legacy_identity_invalid")
            try:
                date.fromisoformat(match[1])
            except ValueError:
                raise StateError("automation_legacy_identity_invalid") from None
            numbers.append(int(match[2]))

    runs = _inside(runtime.root, runtime.directory / "runs")
    if runs.exists():
        for path in runs.iterdir():
            _inside(runtime.root, path)
            consider(path.name)
    outboxes = _inside(runtime.root, runtime.directory / "outboxes")
    if outboxes.exists():
        for path in outboxes.rglob("*"):
            _inside(runtime.root, path)
            consider(path.stem if path.suffix == ".json" else path.name)
    trials = runtime.outbox("trials")
    for collection in ("completed", "progress"):
        directory = _inside(runtime.root, trials.directory / collection)
        if directory.exists():
            for path in directory.glob("*.json"):
                value = trials.read(path.stem)
                if set(value) != {"run_id"} or type(value["run_id"]) is not str:
                    raise StateError("automation_legacy_identity_invalid")
                if _TEST_ID.fullmatch(value["run_id"]) is None:
                    raise StateError("automation_legacy_identity_invalid")
                consider(value["run_id"])
    return max(numbers) + 1


def resolve_launch(
    runtime: RuntimeStore, *, report_mode: str, to_address: str,
    today: date, source: Callable[[], str], validate_new_source: Callable[[], None] | None = None,
) -> dict:
    """Resolve/reserve before provider work; source is a local validation callback."""
    with runtime._write_lock:
        if not runtime._owned:
            raise StateError("state_not_owned")
        if runtime.environment != "daily" or type(today) is not date:
            raise StateError("automation_launch_invalid")
        validate_input(report_mode, to_address)
        box = runtime.outbox("automation")
        active = box.read("active", missing_ok=True)
        resume = False
        if active is not None:
            validate_launch(active)
            saved = read_launch(runtime, active["run_id"])
            if saved is not None and saved != active:
                raise StateError("automation_launch_invalid")
            if saved is None and runtime.run(active["run_id"]).directory.exists():
                raise StateError("automation_launch_invalid")
            _retained_context(runtime, active)
            from .email import read_email
            email = runtime.outbox("email")
            record = (
                read_email(email, active["run_id"])
                if email.read(active["run_id"], missing_ok=True) is not None else None
            )
            if record is not None and record.status in _TERMINAL:
                # An outcome rename may have preceded a failed durability flush.
                # Confirm the terminal evidence before it can release this launch.
                email.save_progress(active["run_id"], record.to_private_dict())
            resume = record is None or record.status not in _TERMINAL or (
                active["report_mode"] == report_mode == "official"
                and active["report_date"] == today.isoformat()
            )
            if resume and (
                active["report_mode"] != report_mode or active["to_address"] != to_address
            ):
                raise QualityError("automation_unfinished_input_conflict")
        revision = source()
        if resume:
            if revision != active["source_revision"]:
                raise QualityError("automation_resume_source_changed")
            launch = active
        else:
            rerun = _next_rerun(runtime) if report_mode == "test" else 0
            run_id = f"daily-{today.isoformat()}" + (f"-test-{rerun}" if rerun else "")
            launch = validate_launch({
                "schema_version": "1.0.0", "run_id": run_id, "report_date": today.isoformat(),
                "source_revision": revision, "report_mode": report_mode,
                "to_address": to_address, "rerun": rerun,
            })
            saved = read_launch(runtime, run_id)
            if saved is not None and saved != launch:
                raise QualityError("automation_launch_conflict")
            records = runtime.run(run_id)
            if saved is None and (
                records.directory.exists()
                or runtime.outbox("email").read(run_id, missing_ok=True) is not None
            ):
                # Never retrofit an override onto a manually launched official run.
                raise QualityError("automation_existing_identity_requires_legacy_resume")
            if saved is None and validate_new_source is not None:
                validate_new_source()
        # A failure at either write stops work. A pointer-only interruption resumes
        # this exact identity, not the next number; completed descriptors never change.
        box.save_progress("active", launch)
        box.save_completed("launches/" + launch["run_id"], launch)
        return launch


def validate_email_binding(runtime: RuntimeStore, request) -> None:
    launch = read_launch(runtime, request.delivery_id)
    if launch != request.delivery_binding:
        raise StateError("automation_delivery_binding_mismatch")
    if launch is None:
        return
    validate_launch(launch)
    identity = _identity(runtime, launch["run_id"], launch["report_mode"] == "test")
    _check_identity(request.to_private_dict(), identity)
    private = read_private_recipient(runtime, launch["run_id"], test_run=identity["test_run"])
    recipient = private if request.mode == "failure" else launch["to_address"]
    if request.recipient != recipient:
        raise StateError("automation_delivery_binding_mismatch")
    frozen = runtime.run(launch["run_id"]).read_completed("delivery-inputs", missing_ok=True)
    if frozen is not None:
        report = frozen.get("report")
        if type(report) is not dict or type(report.get("team_report_eligible")) is not bool:
            raise StateError("automation_delivery_binding_mismatch")
        mode = "test" if identity["test_run"] else (
            "official" if report["team_report_eligible"] else "failure"
        )
        if request.mode != mode:
            raise StateError("automation_delivery_binding_mismatch")
