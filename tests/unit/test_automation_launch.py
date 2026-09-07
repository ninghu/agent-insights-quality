from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import json

import pytest

from agent_insights_quality import automation_launch
from agent_insights_quality.automation_launch import (
    read_launch, resolve_launch, validate_input, validate_launch,
)
from agent_insights_quality.delivery_recipient import freeze_private_recipient
from agent_insights_quality.email import (
    EmailRecord, EmailRequest, TEAM_RECIPIENT, claim_email, prepare_email,
    read_email, record_email_outcome,
)
from agent_insights_quality.errors import QualityError
from agent_insights_quality.state import CheckpointError, RecordStore, RuntimeStore
from test_email import result
from test_runner import fake_storage

DAY = date(2026, 9, 4)
SOURCE = "a" * 40
TO = "literal+report@Example.test"
FALLBACK = "private-fallback@example.test"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    fake_storage(monkeypatch)
    monkeypatch.setattr(automation_launch, "_inside", lambda root, path: path)
    with RuntimeStore("daily", root=tmp_path).ownership() as runtime:
        path = runtime.root / "config" / "email-recipient.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": "1.0.0", "purpose": "daily_test", "recipient": FALLBACK,
        }))
        yield runtime


def resolve(runtime, *, mode="test", to=TO, day=DAY, source=SOURCE):
    launch = resolve_launch(
        runtime, report_mode=mode, to_address=to, today=day, source=lambda: source,
    )
    freeze_private_recipient(
        runtime, launch["run_id"], test_run=mode == "test",
        test_to=to if mode == "test" else None,
    )
    return launch


def prepare(runtime, launch, *, status="prepared", failed=False):
    quality, plan = result(failed=failed)
    request = prepare_email(
        runtime.outbox("email"), launch["run_id"], quality, allowed_units=plan,
        report_date=launch["report_date"], test_run=launch["report_mode"] == "test",
        rerun=launch["rerun"], test_recipient=launch["to_address"],
        failure_recipient=FALLBACK, delivery_binding=launch,
    )
    if status != "prepared":
        claim_email(runtime.outbox("email"), launch["run_id"], claim_id="native-app")
    if status not in {"prepared", "claimed"}:
        record_email_outcome(
            runtime.outbox("email"), launch["run_id"], claim_id="native-app",
            outcome=status, provider_result={"synthetic": status},
        )
    return request


def test_all_legacy_run_and_outbox_numbers_reserved_without_adopting_manual_work(runtime):
    for number in range(1, 8):
        runtime.run(f"daily-{DAY}-test-{number}").save_completed("synthetic", {"retained": True})
    runtime.outbox("email").save_progress(f"daily-{DAY}-test-8", {"retained": True})
    runtime.outbox("trials").save_progress(str(DAY), {"run_id": f"daily-{DAY}-test-9"})
    runtime.outbox("private-reports").save_completed(
        f"requests/daily-{DAY - timedelta(days=1)}-test-10", {"retained": True},
    )
    original = {path: path.read_bytes() for path in runtime.directory.rglob("*.json")}
    launch = resolve(runtime)
    assert launch["rerun"] == 11
    assert all(path.read_bytes() == value for path, value in original.items())
    assert read_launch(runtime, launch["run_id"]) == launch
    assert runtime.outbox("automation").read("active") == launch


@pytest.mark.parametrize("status", [None, "prepared", "claimed", "unknown"])
def test_pending_status_and_cross_midnight_resume_exact_identity(runtime, status):
    launch = resolve(runtime)
    if status:
        prepare(runtime, launch, status=status)
    assert resolve(runtime, day=DAY + timedelta(days=3)) == launch
    assert len(list(runtime.outbox("automation").directory.rglob("launches/*.json"))) == 1
    for changes in ({"to": "other@example.test"}, {"source": "b" * 40}, {"mode": "official"}):
        with pytest.raises(QualityError, match="automation_.*(conflict|changed)"):
            resolve(runtime, day=DAY + timedelta(days=3), **changes)
    assert runtime.outbox("automation").read("active") == launch


@pytest.mark.parametrize("status", ["accepted", "delivered", "rejected"])
def test_terminal_evidence_allows_new_identity_without_rewriting_old_mail(runtime, status):
    first = resolve(runtime)
    prepare(runtime, first, status=status)
    before = runtime.outbox("email")._path("progress", first["run_id"]).read_bytes()
    second = resolve(runtime, to="new@example.test", source="b" * 40, day=DAY + timedelta(days=1))
    assert second["rerun"] == first["rerun"] + 1
    assert second["source_revision"] != first["source_revision"]
    assert runtime.outbox("email")._path("progress", first["run_id"]).read_bytes() == before


def test_unknown_only_advances_after_actual_reconciliation(runtime):
    first = resolve(runtime)
    prepare(runtime, first, status="unknown")
    assert resolve(runtime) == first
    record_email_outcome(
        runtime.outbox("email"), first["run_id"], claim_id="native-app", outcome="rejected",
        provider_result={"synthetic": "definitive rejection"}, reconciliation=True,
    )
    assert resolve(runtime)["rerun"] == first["rerun"] + 1


@pytest.mark.parametrize("failure_key", ["active", "launches/"])
def test_reservation_checkpoint_failure_never_returns_and_retry_preserves_pointer(runtime, monkeypatch, failure_key):
    save = RecordStore._save
    def fail(records, collection, key, value):
        if records.directory == runtime.outbox("automation").directory and key.startswith(failure_key):
            raise CheckpointError()
        return save(records, collection, key, value)
    monkeypatch.setattr(RecordStore, "_save", fail)
    with pytest.raises(CheckpointError):
        resolve(runtime)
    assert not runtime.run(f"daily-{DAY}-test-1").directory.exists()
    assert not runtime.outbox("email").directory.exists()
    monkeypatch.setattr(RecordStore, "_save", save)
    launch = resolve(runtime)
    assert launch["rerun"] == 1


def test_identical_pointer_replay_reestablishes_durability(runtime, monkeypatch):
    launch = resolve(runtime)
    original = RecordStore.save_progress
    def fail(records, key, value):
        if key == "active":
            raise CheckpointError()
        return original(records, key, value)
    monkeypatch.setattr(RecordStore, "save_progress", fail)
    with pytest.raises(CheckpointError):
        resolve(runtime)
    assert runtime.outbox("automation").read("active") == launch


def test_terminal_outcome_must_be_durable_before_allocating_again(runtime, monkeypatch):
    launch = resolve(runtime)
    prepare(runtime, launch, status="accepted")
    original = RecordStore.save_progress
    def fail(records, key, value):
        if records.directory == runtime.outbox("email").directory:
            raise CheckpointError()
        return original(records, key, value)
    monkeypatch.setattr(RecordStore, "save_progress", fail)
    with pytest.raises(CheckpointError):
        resolve(runtime)
    assert runtime.outbox("automation").read("active") == launch
    assert not runtime.run(f"daily-{DAY}-test-2").directory.exists()


@pytest.mark.parametrize("change", [
    {"schema_version": "2"}, {"rerun": True}, {"rerun": 0}, {"rerun": 1.0},
    {"source_revision": "main"}, {"source_revision": []}, {"report_date": "20260904"},
    {"report_date": "2026-02-30"}, {"report_date": None}, {"report_mode": []},
    {"report_mode": "TEST"}, {"report_mode": "failure"}, {"to_address": TO + "\n"},
    {"run_id": f"daily-{DAY}-test-2"}, {"extra": True},
])
def test_invalid_launch_shapes_and_exact_types_block(runtime, change):
    launch = resolve(runtime)
    invalid = {**launch, **change}
    with pytest.raises(QualityError):
        validate_launch(invalid)
    runtime.outbox("automation").save_progress("active", invalid)
    with pytest.raises(QualityError):
        resolve(runtime)


@pytest.mark.parametrize("key,record", [
    ("run", {"kind": "daily", "report_date": str(DAY), "test_run": True, "rerun": True,
             "source_revision": SOURCE}),
    ("traffic-intent", {"fresh_traffic": True, "reuse_run_id": None, "source_revision": "b" * 40}),
    ("traffic-intent", {"fresh_traffic": True, "reuse_run_id": "other", "source_revision": SOURCE}),
    ("delivery-inputs", {"report_date": str(DAY), "test_run": True, "rerun": 1,
                         "source_revision": SOURCE, "recipient": TO}),
])
def test_launch_rejects_conflicting_retained_checkpoint(runtime, key, record):
    launch = resolve(runtime)
    runtime.run(launch["run_id"]).save_completed(key, record)
    with pytest.raises(QualityError):
        resolve(runtime)


@pytest.mark.parametrize("status", ["prepared", "accepted", "rejected"])
def test_official_custom_to_is_frozen_and_date_singleton(runtime, status):
    launch = resolve(runtime, mode="official")
    request = prepare(runtime, launch, status=status)
    assert request.recipient == TO and request.mode == "official" and request.rerun == 0
    assert request.delivery_binding == launch
    assert read_email(runtime.outbox("email"), launch["run_id"]).request == request
    assert resolve(runtime, mode="official") == launch
    with pytest.raises(QualityError):
        resolve(runtime, mode="official", to="other@example.test")
    if status != "prepared":
        new = resolve(runtime, mode="official", day=DAY + timedelta(days=3))
        assert new["run_id"] != launch["run_id"] and new["rerun"] == 0


def test_official_failure_always_uses_frozen_private_fallback(runtime):
    launch = resolve(runtime, mode="official")
    (runtime.root / "config" / "email-recipient.json").unlink()
    assert resolve(runtime, mode="official") == launch
    request = prepare(runtime, launch, failed=True)
    assert request.mode == "failure" and request.recipient == FALLBACK != TO
    assert claim_email(runtime.outbox("email"), launch["run_id"], claim_id="app") == request


def test_unified_official_never_retrofits_an_old_manually_initialized_identity(runtime):
    runtime.run(f"daily-{DAY}").save_completed("delivery-recipient", {"synthetic": "retained"})
    with pytest.raises(QualityError, match="legacy_resume"):
        resolve(runtime, mode="official")
    assert not runtime.outbox("automation").directory.exists()


def test_official_weekend_and_ownership_validation(runtime):
    with pytest.raises(QualityError):
        resolve(runtime, mode="official", day=DAY + timedelta(days=1))
    runtime._owned = False
    try:
        with pytest.raises(QualityError, match="state_not_owned"):
            resolve(runtime)
    finally:
        runtime._owned = True


def test_latest_main_check_only_for_new_official_not_frozen_recovery(runtime):
    calls = []
    def current():
        assert runtime._owned
        return SOURCE
    def fresh():
        calls.append("main")
    launch = resolve_launch(
        runtime, report_mode="official", to_address=TO, today=DAY,
        source=current, validate_new_source=fresh,
    )
    assert calls == ["main"]
    assert resolve_launch(
        runtime, report_mode="official", to_address=TO, today=DAY + timedelta(days=3),
        source=current, validate_new_source=lambda: pytest.fail("Resume replaced frozen main"),
    ) == launch


def test_new_official_source_rejection_leaves_no_reservation(runtime):
    def reject():
        raise QualityError("official_source_not_fetched_main")
    with pytest.raises(QualityError, match="official_source"):
        resolve_launch(
            runtime, report_mode="official", to_address=TO, today=DAY,
            source=lambda: SOURCE, validate_new_source=reject,
        )
    assert not runtime.outbox("automation").directory.exists()


def test_failure_record_cannot_be_relabeled_as_eligible_official(runtime):
    launch = resolve(runtime, mode="official")
    quality, _ = result(failed=True)
    runtime.run(launch["run_id"]).save_completed("delivery-inputs", {
        "report_date": str(DAY), "test_run": False, "rerun": 0,
        "source_revision": SOURCE, "recipient": FALLBACK,
        "delivery_binding": launch, "report": quality.to_dict(),
    })
    request = prepare(runtime, launch, failed=True)
    outbox = runtime.outbox("email")
    outbox.save_progress(launch["run_id"], EmailRecord(
        replace(request, mode="official", recipient=TO), "prepared",
    ).to_private_dict())
    with pytest.raises(QualityError, match="binding"):
        claim_email(outbox, launch["run_id"], claim_id="app")


@pytest.mark.parametrize("mutate", [
    lambda request: replace(request, recipient="spoof@example.test"),
    lambda request: replace(request, recipient=TEAM_RECIPIENT, delivery_binding=None),
    lambda request: replace(request, delivery_binding={**request.delivery_binding, "to_address": "other@example.test"}),
    lambda request: replace(request, delivery_binding={**request.delivery_binding, "source_revision": "b" * 40}),
    lambda request: replace(request, delivery_binding={**request.delivery_binding, "report_date": "2026-09-07"}),
])
def test_tampered_unified_official_request_cannot_read_or_claim(runtime, mutate):
    launch = resolve(runtime, mode="official")
    request = prepare(runtime, launch)
    outbox = runtime.outbox("email")
    outbox.save_progress(launch["run_id"], EmailRecord(mutate(request), "prepared").to_private_dict())
    for action in (
        lambda: read_email(outbox, launch["run_id"]),
        lambda: claim_email(outbox, launch["run_id"], claim_id="app"),
    ):
        with pytest.raises(QualityError):
            action()
    assert outbox.read(launch["run_id"])["status"] == "prepared"


def test_fabricated_descriptor_without_frozen_authorization_cannot_prepare(runtime):
    launch = {
        "schema_version": "1.0.0", "run_id": f"daily-{DAY}", "report_date": str(DAY),
        "source_revision": SOURCE, "report_mode": "official", "to_address": TO, "rerun": 0,
    }
    with pytest.raises(QualityError, match="binding"):
        prepare(runtime, launch)
    assert not runtime.outbox("email").directory.exists()


def test_old_official_serialization_unchanged_and_no_arbitrary_mailbox_loophole(runtime):
    request = EmailRequest(
        f"daily-{DAY}", TEAM_RECIPIENT, "Synthetic subject", "<p>Synthetic</p>",
        "official", str(DAY), False, 0,
    )
    raw = EmailRecord(request, "prepared").to_private_dict()
    assert set(raw["request"]) == {
        "delivery_id", "recipient", "subject", "html", "mode", "report_date", "test_run", "rerun",
    }
    outbox = runtime.outbox("email")
    outbox.save_progress(request.delivery_id, raw)
    before = outbox._path("progress", request.delivery_id).read_bytes()
    assert read_email(outbox, request.delivery_id).request == request
    assert outbox._path("progress", request.delivery_id).read_bytes() == before
    spoofed = deepcopy(raw)
    spoofed["request"]["recipient"] = TO
    outbox.save_progress(request.delivery_id, spoofed)
    with pytest.raises(QualityError, match="isolation"):
        claim_email(outbox, request.delivery_id, claim_id="app")


@pytest.mark.parametrize("mode", ["test", "official"])
@pytest.mark.parametrize("address", [
    None, "", "<TO_ADDRESS>", "TO_ADDRESS@example.test", "${TO_ADDRESS}@example.test",
    "a@example.test,b@example.test", "a@example.test; b@example.test", "Name <a@example.test>",
    "a@example.test\r\nBcc: b@example.test", "a@example.test\n", ["a@example.test"], False,
])
def test_literal_input_validation(mode, address):
    with pytest.raises(QualityError):
        validate_input(mode, address)


def test_mode_not_inferred_by_address():
    with pytest.raises(QualityError):
        validate_input("test", TEAM_RECIPIENT.upper())
    assert validate_input("official", TEAM_RECIPIENT) == TEAM_RECIPIENT
    assert validate_input("official", TO) == TO
