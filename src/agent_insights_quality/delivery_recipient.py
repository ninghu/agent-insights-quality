"""Freeze one human-provided private destination before Daily provider work.

This is the TEST recipient and the official failure-notice fallback. Explicit
official routing is separately bound by automation_launch, never by this default.
"""

from __future__ import annotations

from datetime import date
import re

from .email import TEAM_RECIPIENT, _address, read_email
from .errors import QualityError
from .state import RuntimeStore, StateError

_KEY = "delivery-recipient"
_SOURCES = {
    "explicit_input", "configuration", "delivery_inputs", "email_request", "legacy_explicit_input",
}


def literal_address(value: str | None) -> str:
    address = _address(value)
    local, domain = address.rsplit("@", 1)
    if (
        local.startswith(".") or local.endswith(".") or ".." in local or len(local) > 64
        or any(len(label) > 63 for label in domain.split("."))
    ):
        raise QualityError("email_recipient_invalid")
    if (
        "to_address" in address.casefold()
        or "new_positive_rerun" in address.casefold()
        or any(marker in address for marker in ("{", "}"))
    ):
        raise QualityError("private_recipient_placeholder")
    return address


def _private_address(value: str | None) -> str:
    address = literal_address(value)
    if address.casefold() == TEAM_RECIPIENT.casefold():
        raise QualityError("email_recipient_isolation")
    return address


def validate_test_recipient_input(*, test_run: bool, test_to: str | None = None) -> str | None:
    """Validate literal run input without reading configuration or writing state."""
    if type(test_run) is not bool:
        raise QualityError("runner_test_identity_invalid")
    if test_to is not None and not test_run:
        raise QualityError("test_to_requires_test_run")
    return _private_address(test_to) if test_to is not None else None


def configured_private_recipient(runtime: RuntimeStore) -> str:
    """Read the existing private default; callers must freeze it before work."""
    from .integration import private_path, read_object

    value = read_object(private_path(runtime, runtime.root / "config" / "email-recipient.json"))
    if set(value) != {"schema_version", "purpose", "recipient"} or (
        value["schema_version"] != "1.0.0" or value["purpose"] != "daily_test"
    ):
        raise QualityError("private_recipient_config_invalid")
    return _private_address(value["recipient"])


def _identity(runtime: RuntimeStore, run_id: str, test_run: bool) -> dict:
    match = re.fullmatch(
        r"daily-(\d{4}-\d{2}-\d{2})(?:-test-([1-9][0-9]*))?", run_id,
    ) if isinstance(run_id, str) and len(run_id) <= 80 else None
    if runtime.environment != "daily" or match is None:
        raise QualityError("runner_test_identity_invalid")
    try:
        report_date = date.fromisoformat(match[1]).isoformat()
    except ValueError:
        raise QualityError("runner_test_identity_invalid") from None
    if (match[2] is not None) != test_run:
        raise QualityError("delivery_recipient_identity_mismatch")
    return {"report_date": report_date, "test_run": test_run, "rerun": int(match[2] or 0)}


def _check_identity(value: dict, identity: dict) -> None:
    if any(
        type(value.get(key)) is not type(expected) or value[key] != expected
        for key, expected in identity.items()
    ):
        raise QualityError("delivery_recipient_identity_mismatch")


def read_private_recipient(runtime: RuntimeStore, run_id: str, *, test_run: bool) -> str:
    """Read a strict frozen fallback without configuration or write-side recovery."""
    frozen = runtime.run(run_id).read_completed(_KEY)
    _validate_frozen(frozen, run_id, _identity(runtime, run_id, test_run))
    return _private_address(frozen["recipient"])


def _validate_frozen(frozen: dict, run_id: str, identity: dict) -> None:
    if set(frozen) != {
        "schema_version", "purpose", "run_id", "report_date", "test_run",
        "rerun", "recipient", "source",
    } or (
        frozen["schema_version"] != "1.0.0"
        or frozen["purpose"] != "daily_private_recipient"
        or frozen["run_id"] != run_id
        or not isinstance(frozen["source"], str) or frozen["source"] not in _SOURCES
    ):
        raise QualityError("private_recipient_record_invalid")
    _check_identity(frozen, identity)


def freeze_private_recipient(
    runtime: RuntimeStore, run_id: str, *, test_run: bool, test_to: str | None = None,
) -> str:
    """Return the exact immutable private recipient under runtime ownership.

    A resume ignores mutable defaults. Retained delivery inputs/private email take
    precedence, but conflicting explicit input or historical identities fail.
    A known legacy unfinished TEST run needs explicit input if no recipient was
    retained; unknown legacy identity and missing official fallback fail closed.
    No error includes the input address, and checkpoint failure must stop work.
    """
    with runtime._write_lock:
        if not runtime._owned:
            raise StateError("state_not_owned")
        requested = validate_test_recipient_input(test_run=test_run, test_to=test_to)
        identity = _identity(runtime, run_id, test_run)
        records = runtime.run(run_id)
        frozen = records.read_completed(_KEY, missing_ok=True)
        delivery = records.read_completed("delivery-inputs", missing_ok=True)
        metadata = records.read_completed("run", missing_ok=True)
        outbox = runtime.outbox("email")
        email = (
            read_email(outbox, run_id)
            if outbox.read(run_id, missing_ok=True) is not None else None
        )

        if metadata is not None:
            _check_identity(metadata, identity)
            if metadata.get("kind") != "daily":
                raise QualityError("delivery_recipient_identity_mismatch")
        retained: list[tuple[str, str]] = []
        if frozen is not None:
            _validate_frozen(frozen, run_id, identity)
            retained.append((_private_address(frozen["recipient"]), frozen["source"]))
        if delivery is not None:
            _check_identity(delivery, identity)
            retained.append((_private_address(delivery.get("recipient")), "delivery_inputs"))
        if email is not None:
            _check_identity(email.request.to_private_dict(), identity)
            # read_email already verifies fixed legacy or explicitly bound official routing.
            if email.request.mode != "official":
                retained.append((_private_address(email.request.recipient), "email_request"))

        if retained:
            recipient, source = retained[0]
            if any(address != recipient for address, _ in retained) or (
                requested is not None and requested != recipient
            ):
                raise QualityError("delivery_recipient_conflict")
        else:
            try:
                has_state = records.directory.exists() and any(records.directory.iterdir())
            except OSError as error:
                raise StateError("private_recipient_state_unreadable") from error
            if has_state or email is not None:
                if metadata is None and email is None:
                    raise QualityError("delivery_recipient_identity_unknown")
                if requested is None:
                    raise QualityError("private_recipient_legacy_unfrozen")
                recipient, source = requested, "legacy_explicit_input"
            elif requested is not None:
                recipient, source = requested, "explicit_input"
            else:
                recipient, source = configured_private_recipient(runtime), "configuration"
        record = {
            "schema_version": "1.0.0", "purpose": "daily_private_recipient",
            "run_id": run_id, **identity, "recipient": recipient, "source": source,
        }
        # Identical saves re-establish durability after an interrupted rename.
        records.save_completed(_KEY, record)
        return recipient
