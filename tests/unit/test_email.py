import pytest

from agent_insights_quality.email import (
    EmailError,
    TEAM_RECIPIENT,
    claim_email,
    prepare_email,
    read_email,
    record_email_outcome,
)
from agent_insights_quality.results import PlannedUnit, UnitId, UnitResult, aggregate_results
from agent_insights_quality.state import RuntimeStore, StateConflict, StateError


def result(*, failed=False):
    unit = UnitId("weather-agent", "issue-001")
    plan = (PlannedUnit(unit, "issue-001"),)
    return aggregate_results(plan, () if failed else (UnitResult(unit),)), plan


def prepare(outbox, *, failed=False, **kwargs):
    quality, plan = result(failed=failed)
    return prepare_email(
        outbox, "synthetic-delivery", quality, allowed_units=plan,
        report_date="2026-09-04", **kwargs,
    )


def test_official_test_and_failure_recipient_isolation(tmp_path):
    for mode in ("official", "test", "failure"):
        runtime = RuntimeStore("daily", root=tmp_path / mode)
        with runtime.ownership():
            request = prepare(
                runtime.outbox("email"), failed=mode == "failure",
                test_run=mode == "test", rerun=1 if mode == "test" else 0,
                test_recipient="private-test@example.test",
                failure_recipient="private-failure@example.test",
            )
        assert request.recipient == {
            "official": TEAM_RECIPIENT, "test": "private-test@example.test",
            "failure": "private-failure@example.test",
        }[mode]
        assert request.mode == mode
        assert ("TEST" in request.subject) == (mode == "test")


@pytest.mark.parametrize("kwargs", [
    {"test_run": True, "rerun": 0, "test_recipient": "test@example.test"},
    {"test_run": True, "rerun": 1},
    {"test_run": True, "rerun": 1, "test_recipient": TEAM_RECIPIENT},
    {"test_run": True, "rerun": 1, "test_recipient": "Team@example.test", "team_recipient": "team@example.test"},
    {"test_run": False, "rerun": 1},
    {"test_run": True, "rerun": 1, "test_recipient": "one@example.test,two@example.test"},
    {"test_run": True, "rerun": 1, "test_recipient": "one@example.test\r\nBcc: two@example.test"},
    {"failed": True, "failure_recipient": TEAM_RECIPIENT},
])
def test_invalid_delivery_mode_never_creates_a_request(tmp_path, kwargs):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        with pytest.raises(EmailError):
            prepare(runtime.outbox("email"), **kwargs)
    assert not list(tmp_path.rglob("*.json"))


def test_claim_before_send_replay_content_conflict_and_acceptance_not_delivery(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        box = runtime.outbox("email")
        request = prepare(box)
        assert prepare(box) == request
        with pytest.raises(StateConflict):
            prepare(box, private_context="Changed content is a different delivery.")
        with pytest.raises(EmailError, match="claim_conflict"):
            record_email_outcome(
                box, request.delivery_id, claim_id="app-1",
                outcome="accepted", provider_result={"status": "accepted"},
            )
        assert claim_email(box, request.delivery_id, claim_id="app-1") == request
        with pytest.raises(EmailError, match="reconciliation_required"):
            claim_email(box, request.delivery_id, claim_id="app-1")
        with pytest.raises(EmailError, match="claim_conflict"):
            claim_email(box, request.delivery_id, claim_id="app-2")
        accepted = record_email_outcome(
            box, request.delivery_id, claim_id="app-1", outcome="accepted",
            provider_result={"status": "accepted", "id": "synthetic-provider-id"},
        )
        assert not accepted.inbox_delivery_confirmed
        assert prepare(box) == request
        with pytest.raises(EmailError, match="already_finalized"):
            claim_email(box, request.delivery_id, claim_id="app-1")
        delivered = record_email_outcome(
            box, request.delivery_id, claim_id="app-1", outcome="delivered",
            provider_result={"status": "delivered", "receipt": "synthetic-receipt"},
            reconciliation=True,
        )
        assert delivered.inbox_delivery_confirmed
        assert read_email(box, request.delivery_id) == delivered


def test_ambiguous_send_requires_reconciliation_and_keeps_exact_request(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        box = runtime.outbox("email")
        request = prepare(box)
        claim_email(box, request.delivery_id, claim_id="app-1")
        unknown = record_email_outcome(
            box, request.delivery_id, claim_id="app-1", outcome="unknown",
            provider_result={"status": "timeout"},
        )
        assert read_email(box, request.delivery_id) == unknown
        with pytest.raises(EmailError, match="reconciliation_required"):
            record_email_outcome(
                box, request.delivery_id, claim_id="app-1",
                outcome="accepted", provider_result={"status": "accepted"},
            )
        reconciled = record_email_outcome(
            box, request.delivery_id, claim_id="app-1", outcome="accepted",
            provider_result={"status": "accepted"}, reconciliation=True,
        )
        assert reconciled.request == request
        assert not reconciled.inbox_delivery_confirmed


def test_private_context_is_escaped_and_test_mode_only_writes_private_outbox(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        request = prepare(
            runtime.outbox("email"), test_run=True, rerun=4,
            test_recipient="test@example.test",
            private_context="<script>synthetic private work-item text</script>",
            warnings=("work_item_unavailable",),
        )
    assert "<script>" not in request.html
    assert "&lt;script&gt;synthetic private" in request.html
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    assert files[0].relative_to(runtime.directory).parts[:3] == ("outboxes", "email", "progress")


def test_no_claim_can_escape_when_checkpoint_fails_or_ownership_is_absent(tmp_path, monkeypatch):
    runtime = RuntimeStore("daily", root=tmp_path)
    box = runtime.outbox("email")
    with pytest.raises(StateError, match="state_not_owned"):
        prepare(box)
    with runtime.ownership():
        request = prepare(box)
        def fail(*args):
            raise StateError("state_checkpoint_failed")
        monkeypatch.setattr(box, "save_progress", fail)
        with pytest.raises(StateError, match="checkpoint_failed"):
            claim_email(box, request.delivery_id, claim_id="app-1")
    assert read_email(box, request.delivery_id).status == "prepared"


def test_terminal_outcome_replay_is_idempotent_but_conflicts_are_not_overwritten(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        box = runtime.outbox("email")
        request = prepare(box)
        claim_email(box, request.delivery_id, claim_id="app-1")
        kwargs = dict(
            claim_id="app-1", outcome="rejected", provider_result={"status": "rejected"},
        )
        original = record_email_outcome(box, request.delivery_id, **kwargs)
        assert record_email_outcome(box, request.delivery_id, **kwargs) == original
        with pytest.raises(EmailError, match="outcome_conflict"):
            record_email_outcome(
                box, request.delivery_id, claim_id="app-1",
                outcome="accepted", provider_result={"status": "accepted"}, reconciliation=True,
            )
