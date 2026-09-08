from copy import deepcopy
from base64 import b64encode
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
from urllib.parse import urlencode

import pytest

from agent_insights_quality import private_publication as module
from agent_insights_quality.contracts import Environment
from agent_insights_quality.private_publication import (
    AzurePrivateReportBlob, BlobSnapshot, LATEST, PrivateReportError,
    PrivateReportOutbox, flush_private_report,
)
from agent_insights_quality.settings import AssessmentSettings
from agent_insights_quality.state import CheckpointError, RecordStore, RuntimeStore
import test_publication as publishing
import test_runner as fake

DAY = "2026-09-04"
RUN = "daily-" + DAY
SOURCE = "a" * 40
ENVIRONMENT = Environment(
    "daily", "synthetic", "synthetic", "https://synthetic.invalid",
    "/synthetic/telemetry", "syntheticstore", "syntheticregistry",
    "swedencentral", "Sweden Central",
)


class FakeBlob:
    def __init__(self, *, runtime=None):
        self.runtime = runtime
        self.blobs, self.writes, self.reads, self.timeouts = {}, [], [], []
        self.closed, self.private, self.serial = False, True, 0
        self.fail_key = self.ambiguous_key = self.before_write = None
        self.thread = threading.get_ident()
        self.signings = []
        self.sign_error = None

    def verify_private(self, *, timeout):
        self.timeouts.append(timeout)
        assert threading.get_ident() == self.thread
        if self.runtime:
            assert self.runtime._owned
        if not self.private:
            raise PrivateReportError("private_report_container_not_private")

    def put(self, key, data):
        self.serial += 1
        self.blobs[key] = BlobSnapshot(data, "etag-" + str(self.serial))

    def read(self, key, *, timeout):
        self.timeouts.append(timeout)
        self.reads.append(key)
        return self.blobs.get(key)

    def write(self, key, data, *, content_type, etag, timeout):
        assert self.private
        self.timeouts.append(timeout)
        self.writes.append((key, data, etag))
        if self.before_write:
            self.before_write(key, data)
        if self.fail_key and key.endswith(self.fail_key):
            raise OSError("synthetic secret credential failure")
        current = self.blobs.get(key)
        if (etag is None and current is not None) or (
            etag is not None and (current is None or current.etag != etag)
        ):
            raise PrivateReportError("private_report_write_unresolved")
        self.put(key, data)
        if self.ambiguous_key and key.endswith(self.ambiguous_key):
            raise OSError("synthetic accepted response lost")

    def close(self):
        self.closed = True

    def sign_read_links(self, keys, *, starts_at, expires_at, timeout):
        self.signings.append((list(keys), starts_at, expires_at))
        if self.sign_error:
            raise self.sign_error
        return {key: synthetic_sas(key, starts_at, expires_at) for key in keys}


def synthetic_sas(key, start, end, *, account="syntheticstore", changes=None):
    query = {
        "sv": "2025-05-05", "spr": "https", "st": start, "se": end, "sr": "b", "sp": "r",
        "skoid": "11111111-1111-1111-1111-111111111111",
        "sktid": "22222222-2222-2222-2222-222222222222",
        "skt": start, "ske": end, "sks": "b", "skv": "2025-05-05",
        "sig": b64encode(b"synthetic-test-signature-not-real!"[:32]).decode(),
    }
    query.update(changes or {})
    return f"https://{account}.blob.core.windows.net/quality-artifacts/{key}?" + urlencode(query)


def seed(runtime, result, plan, *, day=DAY, test_run=False, rerun=1, environment=ENVIRONMENT):
    run_id = "daily-" + day + (f"-test-{rerun}" if test_run else "")
    records = runtime.run(run_id)
    records.save_completed("run", {
        "kind": "daily", "report_date": day, "source_revision": SOURCE,
        "test_run": test_run, "rerun": rerun if test_run else 0,
        "targets": [f"{u.unit_id.agent}/{u.unit_id.logical_version}" for u in plan],
    })
    records.save_completed("environment", asdict(environment))
    records.save_completed("assessment-settings", AssessmentSettings().to_dict())
    records.save_artifact("results/final", result.to_dict())
    records.save_progress("quality-result", {"artifact": "results/final", "source_revision": SOURCE})
    return run_id


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    from test_integration import reviewed_catalog
    fake.fake_storage(monkeypatch)
    root = reviewed_catalog(tmp_path, monkeypatch).root
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    result, plan = publishing.quality()
    def build(*, day=DAY, test_run=False, excluded=0, clock=lambda: 0.0):
        actual = publishing.quality(excluded=excluded)[0] if excluded else result
        run_id = seed(runtime, actual, plan, day=day, test_run=test_run)
        outbox = PrivateReportOutbox(runtime, run_id, clock=clock)
        request = outbox.prepare(
            root, actual, allowed_units=plan, environment=ENVIRONMENT,
            source_revision=SOURCE, report_date=day, test_run=test_run,
        )
        return outbox, request
    with runtime.ownership():
        yield SimpleNamespace(runtime=runtime, root=root, result=result, plan=plan, build=build)


@pytest.mark.parametrize("test_run,excluded,mode", [(False, 0, "official"), (True, 0, "test"), (False, 3, "failure")])
def test_immutable_private_bundle_and_namespace(prepared, test_run, excluded, mode):
    outbox, request = prepared.build(test_run=test_run, excluded=excluded)
    client = FakeBlob(runtime=prepared.runtime)
    status = outbox.flush(client)
    assert status["status"] == "delivered"
    assert request["prefix"].startswith("reports/daily/" + mode + "/")
    assert (LATEST in client.blobs) is (mode == "official")
    assert not prepared.runtime.outbox("publication").directory.exists()
    assert not (prepared.root / "reports").exists()
    receipt = json.loads(Path(status["receipt_path"]).read_text())
    manifest = json.loads(request["files"]["manifest.json"])
    for name in ("report.md", "report.html", "manifest.json"):
        content = request["files"][name].encode("utf-8")
        assert receipt["files"][name]["sha256"] == sha256(content).hexdigest()
        assert client.blobs[request["prefix"] + "/" + name].data == content
        reference = receipt["storage_references"][name]
        assert reference["auth_mode"] == "entra_storage_data_plane"
        assert reference["access_required"] and not reference["human_validation_available"]
    assert 'id="synthetic-agent"' in request["files"]["report.html"]
    assert 'id="synthetic-agent"' in request["files"]["report.md"]
    assert manifest["result_sha256"] and manifest["assessment_sha256"]
    assert not any(term in json.dumps(manifest) for term in ("recipient", "token", "C:\\", "private_detail", "query_url"))
    writes = len(client.writes)
    assert outbox.flush(client) == status
    assert len(client.writes) == writes
    assert all(0 < seconds <= 10 for seconds in client.timeouts)


def test_partial_upload_resumes_frozen_bytes_without_rerender(prepared, monkeypatch):
    outbox, request = prepared.build()
    client = FakeBlob()
    client.fail_key = "report.html"
    assert outbox.flush(client)["status"] == "pending"
    assert request["prefix"] + "/report.md" in client.blobs and LATEST not in client.blobs
    assert Path(outbox.status()["html_path"]).exists()
    original = deepcopy(request)
    monkeypatch.setattr(module, "render_private_markdown", lambda *a, **k: pytest.fail("rerender"))
    client.fail_key = None
    resumed = PrivateReportOutbox(prepared.runtime, RUN)
    assert resumed.request() == original
    assert resumed.flush(client)["status"] == "delivered"
    assert [k for k, *_ in client.writes].count(request["prefix"] + "/report.md") == 1


def test_category_plan_and_result_survive_private_bundle_resume(prepared):
    result, plan = publishing.quality(categorized=True)
    run_id = seed(prepared.runtime, result, plan, test_run=True)
    outbox = PrivateReportOutbox(prepared.runtime, run_id)
    request = outbox.prepare(
        prepared.root, result, allowed_units=plan, environment=ENVIRONMENT,
        source_revision=SOURCE, report_date=DAY, test_run=True,
    )
    assert request["plan"] == [unit.to_dict() for unit in plan]
    assert request["plan"][1]["category"] == "hallucinations"
    assert "category" not in request["plan"][0]
    resumed = PrivateReportOutbox(prepared.runtime, run_id)
    assert resumed.request() == request
    assert resumed.run.read_artifact("results/final") == result.to_dict()
    client = FakeBlob()
    assert resumed.flush(client)["status"] == "delivered"
    assert not prepared.runtime.outbox("publication").directory.exists()


@pytest.mark.parametrize("key", ["report.md", "report.html", "manifest.json", "latest.json"])
def test_ambiguous_accepted_put_reconciles_exact_bytes(prepared, key):
    outbox, _ = prepared.build()
    client = FakeBlob()
    client.ambiguous_key = key
    assert outbox.flush(client)["status"] == "delivered"
    assert len(client.writes) == 6


def test_read_only_reconcile_never_puts_or_rebuilds(prepared):
    outbox, request = prepared.build(test_run=True)
    client = FakeBlob()
    assert outbox.flush(client, read_only=True)["status"] == "pending"
    assert not client.writes
    for name, value in request["files"].items():
        client.put(request["prefix"] + "/" + name, value.encode("utf-8"))
    status = flush_private_report(
        prepared.runtime, outbox.run_id, read_only=True, blob_factory=lambda _: client,
    )
    assert status["status"] == "delivered" and not client.writes
    assert client.closed


def test_old_retry_never_rolls_back_official_latest(prepared):
    old, _ = prepared.build()
    newer, request = prepared.build(day="2026-09-07")
    client = FakeBlob()
    assert newer.flush(client)["status"] == "delivered"
    latest = client.blobs[LATEST]
    assert old.flush(client)["latest"] == "superseded"
    assert client.blobs[LATEST] == latest
    assert json.loads(latest.data)["presentation_id"] == request["presentation_id"]


def test_private_test_retains_official_latest_and_receipt_unchanged(prepared):
    official, _ = prepared.build()
    trial, _ = prepared.build(test_run=True)
    client = FakeBlob()
    status = official.flush(client)
    latest = client.blobs[LATEST]
    receipt = Path(status["receipt_path"]).read_bytes()
    assert trial.flush(client)["status"] == "delivered"
    assert client.blobs[LATEST] == latest
    assert Path(status["receipt_path"]).read_bytes() == receipt


def test_latest_cas_race_does_not_overwrite_newer_pointer(prepared):
    old, _ = prepared.build()
    newer, _ = prepared.build(day="2026-09-07")
    client = FakeBlob()
    raced = False
    def race(key, data):
        nonlocal raced
        if key == LATEST and not raced:
            raced = True
            newer.flush(client)
    client.before_write = race
    assert old.flush(client)["latest"] == "superseded"
    assert json.loads(client.blobs[LATEST].data)["report_date"] == "2026-09-07"
    assert all(etag is None for key, _, etag in client.writes if key != LATEST)


@pytest.mark.parametrize("key", ["report.md", "manifest.json", "latest.json"])
def test_conflicting_content_is_visible_never_overwritten(prepared, key):
    outbox, request = prepared.build()
    client = FakeBlob()
    path = LATEST if key == "latest.json" else request["prefix"] + "/" + key
    client.put(path, b"foreign content")
    prior = client.blobs[path]
    status = outbox.flush(client)
    assert status["status"] == "conflict"
    assert client.blobs[path] == prior
    assert path not in [key for key, *_ in client.writes]


def test_public_container_refused_before_any_blob_write(prepared):
    outbox, _ = prepared.build()
    client = FakeBlob()
    client.private = False
    status = outbox.flush(client)
    assert status["code"] == "private_report_container_not_private"
    assert not client.writes and not client.reads
    assert Path(status["markdown_path"]).exists()


@pytest.mark.parametrize("change", ["source", "plan", "result", "assessment", "environment"])
def test_retained_source_plan_result_assessment_and_destination_bound(prepared, monkeypatch, change):
    outbox, request = prepared.build()
    read = RecordStore.read_completed
    def changed(records, key, **kwargs):
        value = read(records, key, **kwargs)
        if records.directory == outbox.run.directory:
            if key == "run" and change in {"source", "plan"}:
                value = deepcopy(value)
                value["source_revision" if change == "source" else "targets"] = "b" * 40 if change == "source" else []
            if key == "assessment-settings" and change == "assessment":
                value = {**value, "deployment_name": "different"}
            if key == "environment" and change == "environment":
                value = {**value, "storage_account_name": "different"}
        return value
    monkeypatch.setattr(RecordStore, "read_completed", changed)
    if change == "result":
        original = RecordStore.read_artifact
        monkeypatch.setattr(RecordStore, "read_artifact", lambda s, k, **kw: (
            publishing.quality(excluded=3)[0].to_dict() if k == "results/final" else original(s, k, **kw)
        ))
    with pytest.raises((PrivateReportError, module.QualityError)):
        outbox.request()
    assert request["identity"]["source_revision"] == SOURCE


def test_wrong_caller_result_and_plan_rejected_before_freezing(prepared):
    seed(prepared.runtime, prepared.result, prepared.plan)
    outbox = PrivateReportOutbox(prepared.runtime, RUN)
    for source, plan, result in (
        ("b" * 40, prepared.plan, prepared.result),
        (SOURCE, prepared.plan[:-1], prepared.result),
        (SOURCE, prepared.plan, publishing.quality(excluded=3)[0]),
    ):
        with pytest.raises(module.QualityError):
            outbox.prepare(
                prepared.root, result, allowed_units=plan, environment=ENVIRONMENT,
                source_revision=source, report_date=DAY, test_run=False,
            )
    assert not outbox.records.directory.exists()


def test_deadline_prevents_further_puts_and_leaves_local_report(prepared):
    ticks = iter([0, 1, 2, 91])
    outbox, _ = prepared.build(clock=lambda: next(ticks, 91))
    client = FakeBlob()
    status = outbox.flush(client)
    assert status["code"] == "private_report_deadline"
    assert not client.writes
    assert Path(status["markdown_path"]).exists()


@pytest.mark.parametrize("phase", ["intent", "receipt"])
def test_checkpoint_failure_stops_publication_not_current_measurement(prepared, monkeypatch, phase):
    outbox, _ = prepared.build()
    client = FakeBlob()
    original = RecordStore._save
    def fail(records, collection, key, value):
        if records.directory == outbox.records.directory and key.endswith("/" + phase):
            raise CheckpointError()
        original(records, collection, key, value)
    monkeypatch.setattr(RecordStore, "_save", fail)
    with pytest.raises(CheckpointError):
        outbox.flush(client)
    assert LATEST not in client.blobs
    assert len(client.writes) == (0 if phase == "intent" else 5)
    with pytest.raises(PrivateReportError, match="checkpoint_failed"):
        outbox.flush(client)
    assert outbox.run.read_artifact("results/final") == prepared.result.to_dict()


@pytest.mark.parametrize("receipt", [None, {"files": {"report.md": "unverified"}}])
def test_delivered_status_requires_matching_immutable_readback_receipt(prepared, monkeypatch, receipt):
    outbox, _ = prepared.build()
    client = FakeBlob()
    assert outbox.flush(client)["status"] == "delivered"
    original = RecordStore.read_completed
    def damaged(records, key, **kwargs):
        if key.endswith("/receipt"):
            return receipt
        return original(records, key, **kwargs)
    monkeypatch.setattr(RecordStore, "read_completed", damaged)
    with pytest.raises(PrivateReportError, match="receipt_"):
        outbox.flush(client)


def test_provider_metadata_capture_is_bounded_and_never_prints_credentials(monkeypatch, capsys):
    monkeypatch.setattr("agent_insights_quality.bootstrap.azure_cli", lambda: "az")
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout=b"secret-token", stderr=b"secret-header")
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(PrivateReportError, match="account_privacy_unverified"):
        module._account_public_access("syntheticstore", 3)
    assert calls[0][1]["capture_output"] and calls[0][1]["timeout"] == 3
    assert calls[0][0][1:4] == ["storage", "account", "list"]
    from agent_insights_quality.bootstrap import RESOURCE_GROUP
    assert calls[0][0][4:6] == ["--resource-group", RESOURCE_GROUP]
    assert not capsys.readouterr().out


def test_no_account_endpoint_or_recipient_can_be_injected_through_name():
    for account in ("https://private.invalid", "a?sig=secret", "storage/../deployment-registries"):
        with pytest.raises(PrivateReportError):
            AzurePrivateReportBlob(account)


def test_recovery_needs_no_source_tree(prepared, monkeypatch):
    outbox, _ = prepared.build()
    monkeypatch.setattr(module, "load_report_context", lambda *a, **k: pytest.fail("source read"))
    monkeypatch.setattr(module, "RetainedReviewContext", lambda *a, **k: pytest.fail("reassessment read"))
    client = FakeBlob()
    status = flush_private_report(prepared.runtime, RUN, blob_factory=lambda _: client)
    assert status["status"] == "delivered" and client.closed
    assert outbox.status()["status"] == "delivered"


def test_lost_put_reply_and_failed_readback_reconcile_without_second_put(prepared):
    outbox, request = prepared.build()
    class LostReply(FakeBlob):
        def read(self, key, **kwargs):
            if len(self.writes) == 1 and self.fail_key == "readback":
                raise PrivateReportError("private_report_read_failed")
            return super().read(key, **kwargs)
    client = LostReply()
    client.ambiguous_key, client.fail_key = "report.md", "readback"
    assert outbox.flush(client)["status"] == "pending"
    assert not outbox.status()["receipt_path"]
    client.fail_key = None
    assert outbox.flush(client)["status"] == "delivered"
    assert [key for key, *_ in client.writes].count(request["prefix"] + "/report.md") == 1


def test_different_content_same_official_date_is_latest_conflict(prepared):
    outbox, request = prepared.build()
    client = FakeBlob()
    foreign = {
        "schema_version": "1.0", "report_date": DAY, "run_id": RUN,
        "presentation_id": "f" * 64, "manifest_sha256": "e" * 64,
        "manifest_key": f"reports/daily/official/{RUN}/{'f' * 64}/manifest.json",
    }
    client.put(LATEST, json.dumps(foreign).encode())
    status = outbox.flush(client)
    assert status["code"] == "private_report_latest_conflict"
    assert request["prefix"] + "/manifest.json" in client.blobs
    assert LATEST not in [key for key, *_ in client.writes]
    assert status["receipt_path"], "Immutable report receipt remains valid despite latest conflict"


@pytest.fixture
def sdk(monkeypatch):
    class AzureError(Exception):
        pass
    class NotFound(AzureError):
        error_code = "BlobNotFound"
    modules = {name: ModuleType(name) for name in (
        "azure", "azure.core", "azure.core.exceptions", "azure.identity", "azure.storage", "azure.storage.blob",
    )}
    modules["azure.core"].MatchConditions = SimpleNamespace(IfNotModified="IfNotModified")
    modules["azure.core.exceptions"].AzureError = AzureError
    modules["azure.core.exceptions"].ResourceNotFoundError = NotFound
    modules["azure.core.exceptions"].HttpResponseError = AzureError
    modules["azure.storage.blob"].ContentSettings = lambda **kwargs: kwargs
    captured = []
    class Credential:
        def __init__(self, **kwargs):
            captured.append(("credential", kwargs))
        def close(self):
            captured.append(("credential_close", {}))
    class Blob:
        def download_blob(self, *, offset, length, **kwargs):
            captured.append(("download", {"offset": offset, "length": length, **kwargs}))
            return SimpleNamespace(readall=lambda: b"content", properties=SimpleNamespace(etag="etag-1"))
        def upload_blob(self, data, **kwargs):
            captured.append(("upload", kwargs))
    class Container:
        properties = {"public_access": None}
        def get_container_properties(self, **kwargs):
            captured.append(("properties", kwargs))
            return self.properties
        def get_blob_client(self, key):
            module._key(key)
            return blob
    blob, container = Blob(), Container()
    class Service:
        def __init__(self, **kwargs):
            captured.append(("service", kwargs))
            self.account_name = "syntheticstore"
            self.url = kwargs["account_url"]
        def get_container_client(self, name):
            assert name == "quality-artifacts"
            return container
        def close(self):
            captured.append(("service_close", {}))
    modules["azure.identity"].AzureCliCredential = Credential
    modules["azure.storage.blob"].BlobServiceClient = Service
    for name, value in modules.items():
        monkeypatch.setitem(sys.modules, name, value)
    return SimpleNamespace(captured=captured, container=container, blob=blob, AzureError=AzureError)


def test_sdk_conditional_writes_bounded_reads_and_identity_only(sdk):
    client = AzurePrivateReportBlob("syntheticstore", account_public_access=lambda *a: False)
    key = f"reports/daily/official/{RUN}/{'a' * 64}/report.md"
    with pytest.raises(PrivateReportError, match="privacy_unverified"):
        client.write(key, b"content", content_type="text/markdown", etag=None, timeout=3)
    client.verify_private(timeout=5)
    client.write(key, b"content", content_type="text/markdown", etag=None, timeout=3)
    client.write(LATEST, b"content", content_type="application/json", etag="saved-etag", timeout=3)
    assert client.read(key, timeout=2) == BlobSnapshot(b"content", "etag-1")
    with pytest.raises(PrivateReportError, match="write_invalid"):
        client.write(key, b"other", content_type="text/markdown", etag="saved-etag", timeout=3)
    client.close()
    uploads = [args for name, args in sdk.captured if name == "upload"]
    assert uploads[0]["overwrite"] is False
    assert uploads[1]["overwrite"] is True
    assert uploads[1]["etag"] == "saved-etag" and uploads[1]["match_condition"] == "IfNotModified"
    assert all(options["retry_total"] == 0 and options["logging_enable"] is False for options in uploads)
    download = next(args for name, args in sdk.captured if name == "download")
    assert download["offset"] == 0
    assert download["length"] == module.MAX_BYTES + 1 and download["max_concurrency"] == 1
    assert ("service_close", {}) in sdk.captured and ("credential_close", {}) in sdk.captured


@pytest.mark.parametrize("properties", [{}, {"public_access": "blob"}, {"public_access": "container"}, {"public_access": ""}])
def test_sdk_refuses_missing_or_public_container_metadata(sdk, properties):
    sdk.container.properties = properties
    client = AzurePrivateReportBlob("syntheticstore", account_public_access=lambda *a: False)
    with pytest.raises(PrivateReportError, match="container_not_private"):
        client.verify_private(timeout=5)
    assert not [args for name, args in sdk.captured if name == "upload"]
    client.close()


@pytest.mark.parametrize("value", [None, True, 0, "false"])
def test_sdk_requires_explicit_account_public_access_disabled(sdk, value):
    client = AzurePrivateReportBlob("syntheticstore", account_public_access=lambda *a: value)
    with pytest.raises(PrivateReportError, match="account_privacy_unverified"):
        client.verify_private(timeout=5)
    assert not sdk.captured


def test_sdk_errors_never_format_credentials_or_raw_response(sdk, monkeypatch, capsys):
    client = AzurePrivateReportBlob("syntheticstore", account_public_access=lambda *a: False)
    def fail(**kwargs):
        raise sdk.AzureError("secret Bearer token and HTTP headers")
    monkeypatch.setattr(sdk.container, "get_container_properties", fail)
    with pytest.raises(PrivateReportError) as error:
        client.verify_private(timeout=5)
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__
    assert not capsys.readouterr().out
    client.close()
