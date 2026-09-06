import json
import subprocess
import sys

import pytest

from agent_insights_quality import delivery_recipient, state
from agent_insights_quality.delivery_recipient import (
    configured_private_recipient,
    freeze_private_recipient,
    validate_test_recipient_input,
)
from agent_insights_quality.email import EmailRecord, EmailRequest, TEAM_RECIPIENT, read_email
from agent_insights_quality.errors import QualityError
from agent_insights_quality.state import CheckpointError, RuntimeStore, StateError

DAY = "2026-09-06"
TEST_RUN = f"daily-{DAY}-test-1"
OFFICIAL_RUN = f"daily-{DAY}"
ADDRESS = "Human.Provided+trial@Example.test"
OTHER = "different@example.test"


@pytest.fixture
def runtime(tmp_path):
    with RuntimeStore("daily", root=tmp_path).ownership() as runtime:
        yield runtime


def configure(runtime, recipient=ADDRESS, **changes):
    path = runtime.root / "config" / "email-recipient.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0.0", "purpose": "daily_test", "recipient": recipient, **changes,
    }), encoding="utf-8")
    return path


def freeze(runtime, *, test_to=None, test_run=True, run_id=TEST_RUN):
    return freeze_private_recipient(runtime, run_id, test_run=test_run, test_to=test_to)


def legacy_identity(*, test_run=True, **changes):
    return {"report_date": DAY, "test_run": test_run, "rerun": 1 if test_run else 0, **changes}


def legacy_email(runtime, *, status="prepared", mode="test", recipient=ADDRESS):
    run_id = TEST_RUN if mode == "test" else OFFICIAL_RUN
    request = EmailRequest(
        run_id, recipient, "Synthetic exact subject", "<p>Synthetic exact HTML</p>",
        mode, DAY, mode == "test", 1 if mode == "test" else 0,
    )
    claimed = status != "prepared"
    record = EmailRecord(
        request, status, "synthetic-claim" if claimed else None,
        {"status": status, "synthetic": True} if status not in {"prepared", "claimed"} else None,
    )
    runtime.outbox("email").save_progress(run_id, record.to_private_dict())
    return record


def test_explicit_input_is_literal_and_frozen_only_in_private_completed_record(runtime):
    configure(runtime, OTHER)
    assert freeze(runtime, test_to=ADDRESS) == ADDRESS
    record = runtime.run(TEST_RUN).read_completed("delivery-recipient")
    assert record == {
        "schema_version": "1.0.0", "purpose": "daily_private_recipient",
        "run_id": TEST_RUN, **legacy_identity(), "recipient": ADDRESS, "source": "explicit_input",
    }
    assert list(runtime.directory.rglob("*.json")) == [
        runtime.run(TEST_RUN)._path("completed", "delivery-recipient"),
    ]
    assert not runtime.outbox("email").directory.exists()
    assert not runtime.outbox("events").directory.exists()


@pytest.mark.parametrize("test_run", [True, False])
def test_default_freezes_for_test_or_official_private_failure_fallback(runtime, test_run):
    configure(runtime)
    run_id = TEST_RUN if test_run else OFFICIAL_RUN
    assert freeze(runtime, test_run=test_run, run_id=run_id) == ADDRESS
    assert runtime.run(run_id).read_completed("delivery-recipient")["source"] == "configuration"


@pytest.mark.parametrize("explicit", [True, False])
def test_resume_ignores_changed_missing_or_invalid_default_and_preserves_exact_bytes(runtime, explicit):
    path = configure(runtime)
    assert freeze(runtime, test_to=ADDRESS if explicit else None) == ADDRESS
    frozen_path = runtime.run(TEST_RUN)._path("completed", "delivery-recipient")
    original = frozen_path.read_bytes()
    for content in (json.dumps({"recipient": OTHER}), "not JSON", None):
        if content is None:
            path.unlink()
        else:
            path.write_text(content)
        assert freeze(runtime) == ADDRESS
        assert freeze(runtime, test_to=ADDRESS) == ADDRESS
        assert frozen_path.read_bytes() == original
    with pytest.raises(QualityError, match="delivery_recipient_conflict"):
        freeze(runtime, test_to=OTHER)
    with pytest.raises(QualityError, match="delivery_recipient_conflict"):
        freeze(runtime, test_to=ADDRESS.lower())
    assert frozen_path.read_bytes() == original


def test_distinct_reruns_can_have_different_explicit_recipients(runtime):
    assert freeze(runtime, test_to=ADDRESS) == ADDRESS
    assert freeze(runtime, run_id=f"daily-{DAY}-test-2", test_to=OTHER) == OTHER
    assert freeze(runtime) == ADDRESS


@pytest.mark.parametrize("value", [
    "", " ", "not-an-address", "<TEST_TO_ADDRESS>", "TEST_TO_ADDRESS",
    "TEST_TO_ADDRESS@example.test", "${TEST_TO_ADDRESS}@example.test",
    "{{recipient}}@example.test", "{recipient}@example.test",
    "NEW_POSITIVE_RERUN@example.test", "person@localhost",
    ".one@example.test", "one.@example.test", "one..two@example.test",
    "a" * 65 + "@example.test", "one@" + "a" * 64 + ".test",
    "one@example.test,two@example.test", "one@example.test;two@example.test",
    "one@example.test two@example.test", "Person <one@example.test>",
    "one@example.test\r\nBcc: two@example.test", "one@example.test\n", " one@example.test",
    "one@example.test ", "one@example.test\x00", "a" * 255 + "@example.test",
    TEAM_RECIPIENT, TEAM_RECIPIENT.upper(), ["one@example.test"], 123, False,
])
def test_unsafe_explicit_input_fails_without_freezing_or_reading_defaults(runtime, value, monkeypatch):
    monkeypatch.setattr(
        delivery_recipient, "configured_private_recipient",
        lambda *_: pytest.fail("Invalid explicit input consulted the default"),
    )
    with pytest.raises(QualityError) as error:
        freeze(runtime, test_to=value)
    assert "@" not in str(error.value)
    assert not list(runtime.directory.rglob("*.json"))


@pytest.mark.parametrize("value", [ADDRESS, "", TEAM_RECIPIENT, "<TEST_TO_ADDRESS>"])
def test_official_mode_rejects_test_to_even_when_no_record_exists(runtime, value):
    with pytest.raises(QualityError, match="test_to_requires_test_run"):
        freeze(runtime, test_run=False, run_id=OFFICIAL_RUN, test_to=value)
    assert not list(runtime.directory.rglob("*.json"))


@pytest.mark.parametrize("changes", [
    {"schema_version": "1.0"}, {"purpose": "official"}, {"extra": True},
    {"recipient": ""}, {"recipient": TEAM_RECIPIENT}, {"recipient": "<TEST_TO_ADDRESS>"},
    {"recipient": "one@example.test;two@example.test"},
])
def test_default_uses_existing_strict_private_schema(runtime, changes):
    configure(runtime, **changes)
    with pytest.raises(QualityError):
        freeze(runtime)
    assert not list(runtime.directory.rglob("*.json"))


def test_configuration_reader_rejects_missing_duplicate_and_non_object_json(runtime):
    with pytest.raises(QualityError, match="private_input_invalid"):
        configured_private_recipient(runtime)
    path = configure(runtime)
    for content in ('{"recipient":"one@example.test","recipient":"two@example.test"}', "[]", "null"):
        path.write_text(content)
        with pytest.raises(QualityError, match="private_input_invalid"):
            configured_private_recipient(runtime)


@pytest.mark.parametrize("run_id,test_run", [
    (TEST_RUN, False), (OFFICIAL_RUN, True), ("daily-2026-02-30-test-1", True),
    (f"daily-{DAY}-test-0", True), (f"daily-{DAY}-test--1", True),
    (f"daily-{DAY}-test-01", True), ("unknown", True), ("daily-2026-09-06-test-1/other", True),
    (TEST_RUN, 1), (TEST_RUN, None),
])
def test_identity_and_mode_mismatch_are_not_defaulted(runtime, run_id, test_run):
    with pytest.raises(QualityError):
        freeze(runtime, run_id=run_id, test_run=test_run, test_to=ADDRESS)
    assert not list(runtime.directory.rglob("*.json"))


def test_staging_is_not_a_delivery_recipient_environment(tmp_path):
    with RuntimeStore("staging", root=tmp_path).ownership() as runtime:
        with pytest.raises(QualityError, match="runner_test_identity_invalid"):
            freeze(runtime, test_to=ADDRESS)


@pytest.mark.parametrize("test_run", [True, False])
def test_legacy_delivery_inputs_preserve_recipient_without_default(runtime, test_run):
    run_id = TEST_RUN if test_run else OFFICIAL_RUN
    records = runtime.run(run_id)
    inputs = {**legacy_identity(test_run=test_run), "recipient": ADDRESS, "report": {"synthetic": True}}
    records.save_completed("delivery-inputs", inputs)
    original = records._path("completed", "delivery-inputs").read_bytes()
    assert freeze(runtime, test_run=test_run, run_id=run_id) == ADDRESS
    assert records.read_completed("delivery-recipient")["source"] == "delivery_inputs"
    assert records._path("completed", "delivery-inputs").read_bytes() == original
    if test_run:
        with pytest.raises(QualityError, match="delivery_recipient_conflict"):
            freeze(runtime, test_to=OTHER)


@pytest.mark.parametrize("status", ["prepared", "claimed", "accepted", "delivered", "unknown", "rejected"])
@pytest.mark.parametrize("mode", ["test", "failure"])
def test_legacy_email_is_exactly_preserved_in_every_send_state(runtime, status, mode):
    original = legacy_email(runtime, status=status, mode=mode)
    run_id = original.request.delivery_id
    path = runtime.outbox("email")._path("progress", run_id)
    original_bytes = path.read_bytes()
    configure(runtime, OTHER)
    assert freeze(runtime, test_run=mode == "test", run_id=run_id) == ADDRESS
    assert runtime.run(run_id).read_completed("delivery-recipient")["source"] == "email_request"
    assert read_email(runtime.outbox("email"), run_id) == original
    assert path.read_bytes() == original_bytes
    if mode == "test":
        with pytest.raises(QualityError, match="delivery_recipient_conflict"):
            freeze(runtime, test_to=OTHER)
        assert path.read_bytes() == original_bytes


def test_legacy_official_team_email_does_not_become_private_fallback(runtime):
    original = legacy_email(runtime, mode="official", recipient=TEAM_RECIPIENT)
    configure(runtime, OTHER)
    with pytest.raises(QualityError, match="private_recipient_legacy_unfrozen"):
        freeze(runtime, test_run=False, run_id=OFFICIAL_RUN)
    runtime.run(OFFICIAL_RUN).save_completed("delivery-inputs", {
        **legacy_identity(test_run=False), "recipient": ADDRESS,
    })
    assert freeze(runtime, test_run=False, run_id=OFFICIAL_RUN) == ADDRESS
    assert read_email(runtime.outbox("email"), OFFICIAL_RUN) == original


@pytest.mark.parametrize("key", ["run", "delivery-inputs", "delivery-recipient"])
@pytest.mark.parametrize("changes", [
    {"test_run": False}, {"rerun": 2}, {"rerun": True}, {"report_date": "2026-09-05"},
])
def test_conflicting_legacy_or_frozen_identity_fails_before_freeze(runtime, key, changes):
    value = {
        "schema_version": "1.0.0", "purpose": "daily_private_recipient",
        "run_id": TEST_RUN, **legacy_identity(**changes), "recipient": ADDRESS, "source": "explicit_input",
    } if key == "delivery-recipient" else {
        **legacy_identity(**changes), "recipient": ADDRESS, **({"kind": "daily"} if key == "run" else {}),
    }
    runtime.run(TEST_RUN).save_completed(key, value)
    with pytest.raises(QualityError, match="delivery_recipient_identity_mismatch"):
        freeze(runtime, test_to=ADDRESS)


@pytest.mark.parametrize("key", ["run", "delivery-inputs"])
def test_legacy_missing_identity_fields_are_not_inferred_from_run_name(runtime, key):
    runtime.run(TEST_RUN).save_completed(key, {"recipient": ADDRESS})
    with pytest.raises(QualityError, match="delivery_recipient_identity_mismatch"):
        freeze(runtime, test_to=ADDRESS)


def test_legacy_unfinished_test_requires_human_input_not_current_default(runtime):
    configure(runtime, OTHER)
    runtime.run(TEST_RUN).save_completed("run", {"kind": "daily", **legacy_identity()})
    with pytest.raises(QualityError, match="private_recipient_legacy_unfrozen"):
        freeze(runtime)
    assert freeze(runtime, test_to=ADDRESS) == ADDRESS
    assert runtime.run(TEST_RUN).read_completed("delivery-recipient")["source"] == "legacy_explicit_input"
    assert freeze(runtime) == ADDRESS


def test_legacy_unfinished_official_cannot_invent_a_private_fallback(runtime):
    configure(runtime)
    runtime.run(OFFICIAL_RUN).save_completed("run", {"kind": "daily", **legacy_identity(test_run=False)})
    with pytest.raises(QualityError, match="private_recipient_legacy_unfrozen"):
        freeze(runtime, test_run=False, run_id=OFFICIAL_RUN)


def test_legacy_interrupted_startup_without_identity_is_blocked_even_with_explicit_input(runtime):
    runtime.run(TEST_RUN).save_completed("traffic-intent", {
        "fresh_traffic": True, "reuse_run_id": None, "source_revision": "a" * 40,
    })
    with pytest.raises(QualityError, match="delivery_recipient_identity_unknown"):
        freeze(runtime, test_to=ADDRESS)
    assert runtime.run(TEST_RUN).read_completed("delivery-recipient", missing_ok=True) is None


def test_disagreeing_historical_sources_cannot_reroute_a_request(runtime):
    original = legacy_email(runtime)
    runtime.run(TEST_RUN).save_completed("delivery-inputs", {**legacy_identity(), "recipient": OTHER})
    with pytest.raises(QualityError, match="delivery_recipient_conflict"):
        freeze(runtime, test_to=ADDRESS)
    assert read_email(runtime.outbox("email"), TEST_RUN) == original
    assert runtime.run(TEST_RUN).read_completed("delivery-recipient", missing_ok=True) is None


def test_later_email_conflict_cannot_override_an_early_freeze(runtime):
    freeze(runtime, test_to=ADDRESS)
    original = legacy_email(runtime, recipient=OTHER)
    with pytest.raises(QualityError, match="delivery_recipient_conflict"):
        freeze(runtime)
    assert read_email(runtime.outbox("email"), TEST_RUN) == original
    assert runtime.run(TEST_RUN).read_completed("delivery-recipient")["recipient"] == ADDRESS


@pytest.mark.parametrize("changes", [
    {"schema_version": "unknown"}, {"purpose": "official"},
    {"source": []}, {"source": "inferred_from_chat"}, {"run_id": OFFICIAL_RUN},
    {"recipient": TEAM_RECIPIENT}, {"extra": "synthetic"},
])
def test_invalid_frozen_record_does_not_fall_back_or_rewrite(runtime, changes):
    value = {
        "schema_version": "1.0.0", "purpose": "daily_private_recipient",
        "run_id": TEST_RUN, **legacy_identity(), "recipient": ADDRESS, "source": "explicit_input",
        **changes,
    }
    configure(runtime, OTHER)
    runtime.run(TEST_RUN).save_completed("delivery-recipient", value)
    with pytest.raises(QualityError):
        freeze(runtime)
    assert runtime.run(TEST_RUN).read_completed("delivery-recipient") == value


@pytest.mark.parametrize("already_frozen", [True, False])
def test_ownership_is_required_even_for_resume(runtime, already_frozen):
    if already_frozen:
        freeze(runtime, test_to=ADDRESS)
    unowned = RuntimeStore("daily", root=runtime.root)
    with pytest.raises(StateError, match="state_not_owned"):
        freeze(unowned, test_to=ADDRESS)


@pytest.mark.parametrize("already_frozen", [True, False])
def test_checkpoint_failure_never_returns_a_recipient(runtime, monkeypatch, already_frozen):
    if already_frozen:
        freeze(runtime, test_to=ADDRESS)

    def fail(*args):
        raise CheckpointError()

    monkeypatch.setattr(state, "_confirm_durable" if already_frozen else "_atomic_write", fail)
    with pytest.raises(CheckpointError, match="state_checkpoint_failed"):
        freeze(runtime, test_to=ADDRESS)
    assert not runtime.outbox("email").directory.exists()


def test_interrupted_durability_after_rename_cannot_redirect_retry(runtime, monkeypatch):
    configure(runtime)
    original = state._replace

    def renamed_then_failed(source, destination):
        original(source, destination)
        raise OSError("synthetic storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(state, "_replace", renamed_then_failed)
        with pytest.raises(CheckpointError):
            freeze(runtime)
    configure(runtime, OTHER)
    assert freeze(runtime) == ADDRESS
    assert not list(runtime.root.rglob(".pending-*"))


def test_preflight_is_pure_and_reuses_email_address_validation(monkeypatch):
    calls = []
    original = delivery_recipient._address

    def address(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(delivery_recipient, "_address", address)
    assert validate_test_recipient_input(test_run=True, test_to=ADDRESS) == ADDRESS
    assert calls == [ADDRESS]
    assert validate_test_recipient_input(test_run=False) is None


def test_helper_import_does_not_load_provider_or_hosted_sdks():
    code = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(('azure', 'agent_framework', 'docker')):
        raise AssertionError('SDK import forbidden')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from agent_insights_quality.delivery_recipient import validate_test_recipient_input
assert validate_test_recipient_input(test_run=True, test_to='synthetic@example.test')
"""
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
