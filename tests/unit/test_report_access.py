from copy import deepcopy
from base64 import b64encode
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_insights_quality import cli, report_access as module
from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.email import (
    EmailError, claim_email, prepare_email, read_email, record_email_outcome,
)
from agent_insights_quality.errors import QualityError
from agent_insights_quality.private_publication import (
    AzurePrivateReportBlob, PrivateReportError, PrivateReportOutbox,
)
from agent_insights_quality.report_access import (
    CLOCK_SKEW, MAX_LIFETIME, ReportAccessError, VerifiedReportAccess,
    prepare_report_access, refresh_report_access, validate_read_sas,
)
from agent_insights_quality.results import PlannedUnit, UnitResult, aggregate_results
from agent_insights_quality.selection import select_daily
from agent_insights_quality.state import CheckpointError, RecordStore, RuntimeStore
from test_private_publication import (
    DAY, ENVIRONMENT, RUN, SOURCE, FakeBlob, seed, synthetic_sas,
)
from test_private_publication import prepared as prepared, sdk as sdk
import test_runner as fake

NOW = datetime(2026, 9, 6, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def grant(prepared, monkeypatch):
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    outbox, request = prepared.build()
    client = FakeBlob()
    assert outbox.flush(client)["status"] == "delivered"
    access = prepare_report_access(outbox, client)
    return SimpleNamespace(
        outbox=outbox, request=request, client=client, access=access, prepared=prepared,
    )


def test_seven_day_cap_includes_skew_and_grant_is_readonly_blob_specific(grant):
    keys, start, expiry = grant.client.signings[0]
    assert module._time(start) == NOW - CLOCK_SKEW
    assert module._time(expiry) == NOW - CLOCK_SKEW + MAX_LIFETIME
    assert module._time(expiry) < NOW + timedelta(days=7)
    assert len(keys) == 2 and all("/agents/synthetic-agent/report." in key for key in keys)
    links = grant.access.for_agents(["synthetic-agent"], RUN)["synthetic-agent"]
    for kind, href in links.items():
        query = parse_qs(urlsplit(href).query)
        assert query["sp"] == ["r"] and query["sr"] == ["b"] and query["spr"] == ["https"]
        assert "sig" in query and "skoid" in query and "sktid" in query
        assert query["se"] == [grant.access.expires_at]
        assert urlsplit(href).path.endswith(".html" if kind == "html" else ".md")
    status = grant.access.status()
    assert status["status"] == "ready" and status["human_validation_available"]
    assert "sig=" not in json.dumps(status)
    assert "sig=" not in repr(grant.access)
    assert "sig=" not in json.dumps(grant.access.descriptor)
    for key, snapshot in grant.client.blobs.items():
        assert "sig=" not in snapshot.data.decode() and "sktid" not in snapshot.data.decode()
    request = grant.outbox.records.read_completed(grant.outbox.request_key)
    assert "sig=" not in json.dumps(request)


@pytest.mark.parametrize("changes", [
    {"sp": "rl"}, {"sp": "w"}, {"sp": "rd"}, {"sr": "c"}, {"spr": "http"},
    {"sks": "f"}, {"sig": "not-base64"}, {"si": "policy"}, {"ss": "b"},
    {"se": "2026-09-14T07:55:00Z"}, {"ske": "2026-09-14T07:55:00Z"},
    {"skt": "2026-09-05T07:55:00Z"},
])
def test_rejects_excess_permissions_account_sas_and_bad_lifetime(grant, changes):
    key, start, end = grant.client.signings[0]
    key = key[0]
    bad = synthetic_sas(key, start, end, changes=changes)
    with pytest.raises(ReportAccessError):
        validate_read_sas(bad, account="syntheticstore", blob_key=key, starts_at=start, expires_at=end)


@pytest.mark.parametrize("mutation", [
    lambda u: u.replace("syntheticstore.", "differentstore."),
    lambda u: u.replace("/quality-artifacts/", "/deployment-registries/"),
    lambda u: u.replace("/synthetic-agent/", "/another-agent/"),
    lambda u: u.replace("/report.html?", "/report.md?"),
    lambda u: u + "&sp=r",
    lambda u: u + "#other-agent",
    lambda u: u.replace("https:", "http:"),
    lambda u: " " + u,
])
def test_rejects_foreign_account_container_file_and_query_ambiguity(grant, mutation):
    keys, start, end = grant.client.signings[0]
    url = synthetic_sas(keys[0], start, end)
    with pytest.raises(ReportAccessError):
        validate_read_sas(mutation(url), account="syntheticstore", blob_key=keys[0], starts_at=start, expires_at=end)


def test_unverified_or_changed_blob_never_gets_signed(prepared, monkeypatch):
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    outbox, request = prepared.build()
    client = FakeBlob()
    with pytest.raises(ReportAccessError, match="publication_incomplete"):
        prepare_report_access(outbox, client)
    outbox.flush(client)
    client.put(request["prefix"] + "/agents/synthetic-agent/report.html", b"foreign")
    with pytest.raises(ReportAccessError, match="blob_unverified"):
        prepare_report_access(outbox, client)
    assert not client.signings


def test_expired_claim_requires_explicit_revision_and_never_rewrites_email(grant, monkeypatch):
    runtime = grant.prepared.runtime
    box = runtime.outbox("email")
    email = prepare_email(
        box, RUN, grant.prepared.result, allowed_units=grant.prepared.plan,
        report_date=DAY, report_access=grant.access,
    )
    assert "View report" in email.html and "Download MD" in email.html
    assert grant.access.expires_at in email.html and "Anyone holding a link" in email.html
    original = box.read(RUN)
    monkeypatch.setattr(module, "utc_now", lambda: module._time(grant.access.expires_at) + timedelta(seconds=1))
    with pytest.raises(EmailError, match="expired_needs_new_revision"):
        claim_email(box, RUN, claim_id="claim-one")
    writes = list(grant.client.writes)
    prepared = refresh_report_access(
        runtime, RUN, revision="refresh-1", blob_factory=lambda _: grant.client,
    )
    assert prepared["status"] == "ready" and Path(prepared["preview_path"]).is_file()
    assert "NOT SENT" in Path(prepared["preview_path"]).read_text()
    assert grant.client.writes == writes and len(grant.client.signings) == 2
    assert box.read(RUN) == original and read_email(box, RUN).status == "prepared"
    with pytest.raises(EmailError, match="expired_needs_new_revision"):
        claim_email(box, RUN, claim_id="claim-two")
    assert refresh_report_access(
        runtime, RUN, revision="refresh-1", blob_factory=lambda _: grant.client,
    ) == prepared
    assert len(grant.client.signings) == 2
    assert "sig=" not in json.dumps(prepared)


def test_sent_request_remains_exact_after_expiry_and_access_refresh(grant, monkeypatch):
    box = grant.prepared.runtime.outbox("email")
    request = prepare_email(
        box, RUN, grant.prepared.result, allowed_units=grant.prepared.plan,
        report_date=DAY, report_access=grant.access,
    )
    claim_email(box, RUN, claim_id="claim")
    sent = record_email_outcome(
        box, RUN, claim_id="claim", outcome="delivered", provider_result={"delivery": "synthetic"},
    )
    monkeypatch.setattr(module, "utc_now", lambda: module._time(grant.access.expires_at) + timedelta(seconds=1))
    assert prepare_report_access(grant.outbox, grant.client).status()["status"].startswith("expired")
    assert len(grant.client.signings) == 1
    with pytest.raises(EmailError, match="already_finalized"):
        claim_email(box, RUN, claim_id="claim")
    refresh_report_access(grant.prepared.runtime, RUN, revision="refresh-2", blob_factory=lambda _: grant.client)
    assert read_email(box, RUN) == sent and sent.request == request
    assert len(grant.client.signings) == 2


def test_delegation_denial_and_interrupted_signing_require_explicit_new_revision(prepared, monkeypatch):
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    outbox, _ = prepared.build()
    client = FakeBlob()
    outbox.flush(client)
    client.sign_error = ReportAccessError("report_access_delegation_denied")
    with pytest.raises(ReportAccessError, match="delegation_denied"):
        prepare_report_access(outbox, client)
    client.sign_error = None
    with pytest.raises(ReportAccessError, match="interrupted_needs_new_revision"):
        prepare_report_access(outbox, client)
    assert len(client.signings) == 1
    assert prepare_report_access(outbox, client, revision="reviewed-retry").status()["status"] == "ready"
    assert len(client.signings) == 2


def test_access_checkpoint_failure_never_persists_delegation_material_or_allows_more_signing(prepared, monkeypatch):
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    outbox, _ = prepared.build()
    client = FakeBlob()
    outbox.flush(client)
    original = RecordStore.save_completed
    def fail(records, key, value):
        if "/access/" in key:
            raise CheckpointError()
        original(records, key, value)
    monkeypatch.setattr(RecordStore, "save_completed", fail)
    with pytest.raises(CheckpointError):
        prepare_report_access(outbox, client)
    with pytest.raises(ReportAccessError, match="checkpoint_failed"):
        prepare_report_access(outbox, client, revision="another")
    assert len(client.signings) == 1
    assert all("sig=" not in path.read_text() for path in outbox.records.directory.rglob("*.json"))


def test_access_record_cannot_swap_files_or_change_descriptor(grant, monkeypatch):
    original = RecordStore.read_completed
    def changed(records, key, **kwargs):
        value = original(records, key, **kwargs)
        if "/access/" in key:
            value = deepcopy(value)
            links = value["links"]["synthetic-agent"]
            links["html"] = links["markdown"]
        return value
    monkeypatch.setattr(RecordStore, "read_completed", changed)
    with pytest.raises(ReportAccessError, match="sas_invalid"):
        VerifiedReportAccess(grant.prepared.runtime, grant.access.descriptor["record_key"])


def damage_report_records(grant, damage):
    records = grant.outbox.records
    base = grant.outbox.run_id + "/" + grant.request["presentation_id"]
    receipt = records._path("completed", base + "/receipt")
    access = records._path("completed", grant.access.descriptor["record_key"])
    request = records._path("completed", grant.outbox.request_key)
    if damage == "missing_receipt":
        receipt.unlink()
    elif damage == "mismatched_receipt":
        value = json.loads(receipt.read_text())
        value["files"]["report.md"]["sha256"] = "f" * 64
        receipt.write_text(json.dumps(value))
    elif damage == "corrupt_receipt":
        receipt.write_text("{invalid receipt")
    elif damage in {"missing_access", "expired_missing_access"}:
        access.unlink()
    elif damage == "corrupt_access":
        access.write_text("{invalid access sig=cached-private-secret")
    elif damage == "mismatched_access":
        value = json.loads(access.read_text())
        links = value["links"][next(iter(value["links"]))]
        key = urlsplit(links["html"]).path.removeprefix("/quality-artifacts/")
        links["html"] = synthetic_sas(
            key, value["starts_at"], value["expires_at"],
            changes={"sig": b64encode(b"x" * 32).decode()},
        )
        access.write_text(json.dumps(value))
    elif damage in {"latest_pending", "latest_conflict"}:
        records.save_progress(base + "/status", {
            "status": "pending" if damage == "latest_pending" else "conflict",
            "latest": "pending" if damage == "latest_pending" else "conflict",
            "code": "private_report_write_unresolved" if damage == "latest_pending" else "private_report_latest_conflict",
        })
    elif damage == "missing_request":
        request.unlink()
    elif damage == "corrupt_request":
        request.write_text("{invalid publication")
    elif damage.startswith("manifest_") or damage == "invalid_files":
        value = json.loads(request.read_text())
        if damage == "invalid_files":
            value["files"] = ["manifest.json"]
        else:
            value["files"]["manifest.json"] = {
                "manifest_list": "[]", "manifest_null": "null",
                "manifest_string": "\"manifest\"", "manifest_boolean": "true",
                "manifest_number": "42",
            }[damage]
        request.write_text(json.dumps(value))
    elif damage == "invalid_status":
        records._path("progress", base + "/status").write_text(json.dumps({
            "status": [], "latest": "advanced", "code": "ok",
        }))


@pytest.mark.parametrize("damage,code", [
    ("missing_receipt", "private_report_receipt_missing"),
    ("mismatched_receipt", "private_report_receipt_invalid"),
    ("corrupt_receipt", "state_record_corrupt"),
])
def test_every_access_restore_read_and_claim_requires_matching_bundle_receipt(grant, monkeypatch, damage, code):
    runtime = grant.prepared.runtime
    box = runtime.outbox("email")
    prepare_email(
        box, RUN, grant.prepared.result, allowed_units=grant.prepared.plan,
        report_date=DAY, report_access=grant.access,
    )
    original = box.read(RUN)
    damage_report_records(grant, damage)
    monkeypatch.setattr(box, "save_progress", lambda *a, **k: pytest.fail("Invalid access reached claim checkpoint"))
    for read in (
        lambda: VerifiedReportAccess(runtime, grant.access.descriptor["record_key"]),
        lambda: module.read_report_access(runtime, grant.access.descriptor, delivery_id=RUN),
        lambda: grant.access.status(),
        lambda: grant.access.for_agents(["synthetic-agent"], RUN),
        lambda: claim_email(box, RUN, claim_id="must-not-claim"),
    ):
        with pytest.raises(QualityError, match=code):
            read()
    assert box.read(RUN) == original and read_email(box, RUN).status == "prepared"


@pytest.mark.parametrize("damage", ["latest_pending", "latest_conflict"])
def test_valid_bundle_allows_read_and_claim_without_latest_advancement(grant, damage):
    box = grant.prepared.runtime.outbox("email")
    request = prepare_email(
        box, RUN, grant.prepared.result, allowed_units=grant.prepared.plan,
        report_date=DAY, report_access=grant.access,
    )
    damage_report_records(grant, damage)
    restored = module.read_report_access(grant.prepared.runtime, request.report_access, delivery_id=RUN)
    assert restored.status()["status"] == "ready"
    assert grant.outbox.status()["status"] in {"pending", "conflict"}
    assert claim_email(box, RUN, claim_id="valid-bundle") == request


@pytest.mark.parametrize("damage,published,available,code", [
    ("fresh", "delivered", "ready", None),
    ("expired", "delivered", "expired_needs_explicit_new_access_revision", None),
    ("missing_receipt", "unavailable", "unavailable", "private_report_receipt_missing"),
    ("mismatched_receipt", "unavailable", "unavailable", "private_report_receipt_invalid"),
    ("corrupt_receipt", "unavailable", "unavailable", "state_record_corrupt"),
    ("missing_access", "delivered", "unavailable", "report_access_record_missing"),
    ("expired_missing_access", "delivered", "unavailable", "report_access_record_missing"),
    ("corrupt_access", "delivered", "unavailable", "report_access_record_invalid"),
    ("mismatched_access", "delivered", "unavailable", "report_access_descriptor_mismatch"),
    ("latest_pending", "pending", "ready", None),
    ("latest_conflict", "conflict", "ready", None),
    ("missing_request", "unavailable", "unavailable", "state_record_missing"),
    ("corrupt_request", "unavailable", "unavailable", "state_record_corrupt"),
    ("manifest_list", "unavailable", "unavailable", "private_report_request_invalid"),
    ("manifest_null", "unavailable", "unavailable", "private_report_request_invalid"),
    ("manifest_string", "unavailable", "unavailable", "private_report_request_invalid"),
    ("manifest_boolean", "unavailable", "unavailable", "private_report_request_invalid"),
    ("manifest_number", "unavailable", "unavailable", "private_report_request_invalid"),
    ("invalid_files", "unavailable", "unavailable", "private_report_request_invalid"),
    ("invalid_status", "unavailable", "unavailable", "private_report_status_invalid"),
])
def test_cli_status_rebuilds_publication_and_access_without_cached_ready_or_side_effects(
    grant, monkeypatch, capsys, damage, published, available, code,
):
    runtime = grant.prepared.runtime
    prepare_email(
        runtime.outbox("email"), RUN, grant.prepared.result, allowed_units=grant.prepared.plan,
        report_date=DAY, report_access=grant.access,
    )
    runtime.run(RUN).save_progress("private-publication", {
        "status": "delivered", "presentation_id": "f" * 64,
        "access": {"status": "ready", "human_validation_available": True,
                   "expires_at": "2100-01-01T00:00:00Z", "url": "?sig=cached-private-secret"},
    })
    if damage.startswith("expired"):
        monkeypatch.setattr(module, "utc_now", lambda: module._time(grant.access.expires_at) + timedelta(seconds=1))
    damage_report_records(grant, damage)
    def forbidden(*args, **kwargs):
        pytest.fail("Read-only status performed provider, renderer, signing or writer work")
    from agent_insights_quality import private_publication, reporting
    monkeypatch.setattr(runtime, "ownership", forbidden)
    monkeypatch.setattr(RecordStore, "_save", forbidden)
    monkeypatch.setattr(private_publication, "_write_local", forbidden)
    monkeypatch.setattr(private_publication, "AzurePrivateReportBlob", forbidden)
    monkeypatch.setattr(module, "AzurePrivateReportBlob", forbidden)
    monkeypatch.setattr(module, "prepare_report_access", forbidden)
    monkeypatch.setattr(reporting, "render_private_markdown", forbidden)
    monkeypatch.setattr(reporting, "markdown_view", forbidden)
    monkeypatch.setattr(cli, "_catalog", forbidden)
    before = {path: path.read_bytes() for path in runtime.directory.rglob("*.json")}
    assert cli.main(
        ["status"], root=grant.prepared.root, runtime_factory=lambda _: runtime,
    ) == 0
    output = capsys.readouterr()
    assert not output.err and "sig=" not in output.out and "cached-private-secret" not in output.out
    row = next(run for run in json.loads(output.out)["runs"] if run["run_id"] == RUN)
    assert row["email_status"] == "prepared" and not row["inbox_delivery_confirmed"]
    publication = row["private_report"]
    assert publication["status"] == published
    assert publication["access"]["status"] == available
    assert publication["access"]["human_validation_available"] is (available == "ready")
    if code:
        assert publication["access"]["code"] == code
    if published != "unavailable":
        assert publication["presentation_id"] == grant.request["presentation_id"]
    if available != "unavailable":
        assert publication["access"]["expires_at"] == grant.access.expires_at
    assert before == {path: path.read_bytes() for path in runtime.directory.rglob("*.json")}


@pytest.mark.parametrize("outcome", ["accepted", "delivered", "unknown"])
@pytest.mark.parametrize("damage", ["missing_receipt", "missing_access"])
def test_historical_send_outcome_is_immutable_and_separate_from_current_link_availability(
    grant, monkeypatch, capsys, outcome, damage,
):
    runtime = grant.prepared.runtime
    box = runtime.outbox("email")
    prepare_email(
        box, RUN, grant.prepared.result, allowed_units=grant.prepared.plan,
        report_date=DAY, report_access=grant.access,
    )
    claim_email(box, RUN, claim_id="recorded-claim")
    sent = record_email_outcome(
        box, RUN, claim_id="recorded-claim", outcome=outcome, provider_result={"status": "synthetic"},
    )
    before = box._path("progress", RUN).read_bytes()
    damage_report_records(grant, damage)
    assert read_email(box, RUN) == sent
    with pytest.raises(EmailError, match="reconciliation_required" if outcome == "unknown" else "already_finalized"):
        claim_email(box, RUN, claim_id="recorded-claim")
    monkeypatch.setattr(runtime, "ownership", lambda: pytest.fail("Status acquired writer"))
    assert cli.main(["status"], root=grant.prepared.root, runtime_factory=lambda _: runtime) == 0
    output = capsys.readouterr()
    row = next(run for run in json.loads(output.out)["runs"] if run["run_id"] == RUN)
    assert row["email_status"] == outcome and row["inbox_delivery_confirmed"] is (outcome == "delivered")
    assert row["private_report"]["access"]["status"] == "unavailable"
    assert not row["private_report"]["access"]["human_validation_available"]
    assert "sig=" not in output.out + output.err
    assert box._path("progress", RUN).read_bytes() == before


def test_zero_fractional_utc_sas_times_are_equivalent_but_never_extend_expiry(grant):
    keys, start, end = grant.client.signings[0]
    url = synthetic_sas(keys[0], start, end, changes={
        "skt": start.replace("Z", ".0000000Z"), "ske": end.replace("Z", ".0000000Z"),
    })
    assert validate_read_sas(
        url, account="syntheticstore", blob_key=keys[0], starts_at=start, expires_at=end,
    ) == url
    with pytest.raises(ReportAccessError):
        validate_read_sas(
            url.replace(".0000000Z", ".0000001Z"), account="syntheticstore",
            blob_key=keys[0], starts_at=start, expires_at=end,
        )


@pytest.mark.parametrize("credential", ["synthetic-account-key", "sig=synthetic", b"key", {}])
def test_account_keys_and_sas_credentials_are_not_an_injection_option(credential):
    with pytest.raises(PrivateReportError, match="token_identity_required"):
        AzurePrivateReportBlob("syntheticstore", credential=credential)


def test_foreign_injected_service_scope_is_rejected(sdk):
    service = SimpleNamespace(
        account_name="anotherstore", url="https://anotherstore.blob.core.windows.net", close=lambda: None,
    )
    client = AzurePrivateReportBlob(
        "syntheticstore", service=service, account_public_access=lambda *a: False,
    )
    with pytest.raises(PrivateReportError, match="service_scope_invalid"):
        client.verify_private(timeout=5)
    client.close()


def test_embedded_sas_in_report_prose_is_never_uploaded(prepared, monkeypatch):
    from agent_insights_quality import private_publication
    seed(prepared.runtime, prepared.result, prepared.plan)
    monkeypatch.setattr(
        private_publication, "render_private_markdown",
        lambda *a, **k: "# Synthetic report\nhttps://synthetic.invalid/file?sig=not-a-real-token\n",
    )
    outbox = PrivateReportOutbox(prepared.runtime, RUN)
    with pytest.raises(PrivateReportError, match="embedded_access_forbidden"):
        outbox.prepare(
            prepared.root, prepared.result, allowed_units=prepared.plan,
            environment=ENVIRONMENT, source_revision=SOURCE, report_date=DAY, test_run=False,
        )
    assert not outbox.records.read_completed(outbox.request_key, missing_ok=True)


def test_five_agent_documents_and_ten_blob_grants_share_one_result(tmp_path, monkeypatch):
    fake.fake_storage(monkeypatch)
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    root = Path(__file__).resolve().parents[2]
    catalog = load_catalog(root)
    selected = select_daily(catalog, date.fromisoformat(DAY))
    plan = tuple(PlannedUnit(t.unit_id, None if t.is_baseline else t.unit_id.logical_version) for t in selected)
    result = aggregate_results(plan, tuple(UnitResult(unit.unit_id) for unit in plan))
    before = result.to_dict()
    runtime = RuntimeStore("daily", root=tmp_path / "runtime")
    with runtime.ownership():
        seed(runtime, result, plan)
        run = runtime.run(RUN)
        for unit in plan:
            key = f"{unit.unit_id.agent}/{unit.unit_id.logical_version}"
            run.save_progress("targets/" + key + "/source", {"traffic_run_id": RUN, "work_key": "work/" + key})
            run.save_completed("work/" + key + "/deployment", {
                "target_key": key, "agent_name": unit.unit_id.agent,
                "provider_version": "synthetic-version-" + unit.unit_id.logical_version,
                "source_revision": SOURCE,
            })
        outbox = PrivateReportOutbox(runtime, RUN)
        request = outbox.prepare(
            root, result, allowed_units=plan, environment=ENVIRONMENT,
            source_revision=SOURCE, report_date=DAY, test_run=False,
        )
        client = FakeBlob()
        outbox.flush(client)
        access = prepare_report_access(outbox, client)
        assert len(client.signings[0][0]) == 10
        assert len(request["files"]) == 13
        for agent, links in access.for_agents(catalog.agents, RUN).items():
            markdown = request["files"][f"agents/{agent}/report.md"]
            html = request["files"][f"agents/{agent}/report.html"]
            assert len([line for line in markdown.splitlines() if line.startswith("| ")]) == 7
            assert "Overall Daily quality score" in markdown and "This Agent (no separate score)" in markdown
            assert "**Assigned To:**" in markdown and "synthetic-version-v0" in markdown
            assert 'id="' + agent + '"' in html
            assert all(f"/agents/{agent}/" in link for link in links.values())
            for other in set(catalog.agents) - {agent}:
                assert f'id="{other}"' not in markdown + html
        assert result.to_dict() == before
        assert all("sig=" not in blob.data.decode() for blob in client.blobs.values())
        assert request["identity"]["result_sha256"] == sha256(module._encode(before)).hexdigest()


def test_sdk_requests_only_user_delegation_key_and_read_blob_sas(sdk, monkeypatch, capsys):
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    client = AzurePrivateReportBlob("syntheticstore", account_public_access=lambda *a: False)
    client.verify_private(timeout=5)
    start = module._stamp(NOW - CLOCK_SKEW)
    end = module._stamp(NOW - CLOCK_SKEW + MAX_LIFETIME)
    delegation = SimpleNamespace(
        signed_start=start, signed_expiry=end, signed_service="b", signed_version="2025-05-05",
        signed_oid="11111111-1111-1111-1111-111111111111",
        signed_tid="22222222-2222-2222-2222-222222222222", value="synthetic-delegation-secret",
    )
    requested, signatures = [], []
    def get_key(**kwargs):
        requested.append(kwargs)
        return delegation
    monkeypatch.setattr(client.service, "get_user_delegation_key", get_key, raising=False)
    blob = sys.modules["azure.storage.blob"]
    monkeypatch.setattr(blob, "BlobSasPermissions", lambda **kwargs: kwargs, raising=False)
    def generate(**kwargs):
        signatures.append(kwargs)
        assert "account_key" not in kwargs
        assert kwargs["user_delegation_key"] is delegation
        return urlsplit(synthetic_sas(kwargs["blob_name"], start, end)).query
    monkeypatch.setattr(blob, "generate_blob_sas", generate, raising=False)
    prefix = f"reports/daily/official/{RUN}/{'a' * 64}/agents/"
    keys = [
        prefix + agent + f"/report.{extension}"
        for agent in load_catalog(Path(__file__).resolve().parents[2]).agents
        for extension in ("html", "md")
    ]
    output = client.sign_read_links(keys, starts_at=start, expires_at=end, timeout=5)
    assert set(output) == set(keys)
    assert requested[0]["key_expiry_time"] - requested[0]["key_start_time"] == timedelta(days=7)
    assert requested[0]["logging_enable"] is False and requested[0]["retry_total"] == 0
    assert all(call["permission"] == {"read": True} and call["protocol"] == "https" for call in signatures)
    assert [call["blob_name"] for call in signatures] == keys
    for bad_end in (module._stamp(NOW + timedelta(days=7)), module._stamp(NOW - timedelta(days=1))):
        with pytest.raises(ReportAccessError, match="lifetime_invalid"):
            client.sign_read_links(keys, starts_at=start, expires_at=bad_end, timeout=5)
    assert len(requested) == 1
    def denied(**kwargs):
        error = sdk.AzureError("synthetic secret response")
        error.status_code = 403
        raise error
    monkeypatch.setattr(client.service, "get_user_delegation_key", denied)
    with pytest.raises(ReportAccessError, match="delegation_denied"):
        client.sign_read_links(keys, starts_at=start, expires_at=end, timeout=5)
    assert not capsys.readouterr().out
    client.close()
