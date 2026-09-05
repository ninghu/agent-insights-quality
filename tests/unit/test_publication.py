import asyncio
import builtins
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from agent_insights_quality.events import RunLogger, project_event
from agent_insights_quality.privacy import PrivacyError, public_projection
from agent_insights_quality.publication import (
    AdxUnavailable,
    AzureCliAdxClient,
    EVENT_TABLE,
    PublicationError,
    PublicationOutbox,
    REPORT_TABLE,
    build_public_report,
    validate_public_report,
)
from agent_insights_quality.results import (
    CardVerdict, CoreVerdict, ExclusionReason, PlannedUnit, UnitId, UnitResult, aggregate_results,
)
from agent_insights_quality.state import CheckpointError, RuntimeStore, StateConflict


RUN = "daily-2026-09-04-r0"
METADATA = {"report_date": "2026-09-04", "source_commit": "a" * 40, "region": "SwedenCentral"}


def quality(excluded=0):
    plan = [PlannedUnit(UnitId("synthetic-agent", "v0"))]
    actual = [UnitResult(plan[0].unit_id, (CardVerdict("card-0001", CoreVerdict.INCORRECT),))]
    for number in range(1, 4):
        alias = f"issue-{number:03}"
        planned = PlannedUnit(UnitId("synthetic-agent", alias), alias)
        plan.append(planned)
        cards = () if number == 2 else (CardVerdict("card-0001", CoreVerdict.CORRECT, alias),)
        if number == 1:
            cards += (CardVerdict("card-0002", CoreVerdict.CORRECT, alias),)
        actual.append(UnitResult(planned.unit_id, cards))
    for index in range(excluded):
        actual[index] = replace(actual[index], exclusion_reasons=(ExclusionReason.INCOMPLETE_EVIDENCE,))
    return aggregate_results(plan, actual), tuple(plan)


def event(number=0):
    return {
        "utc": "2026-09-04T12:00:00.000Z", "elapsed_ms": number,
        "kind": "heartbeat", "stage": "traffic", "code": "ok",
        "counters": {"completed_count": number},
    }


def outbox(runtime, **kwargs):
    return PublicationOutbox(
        runtime.outbox("publication"), framework_run_id=kwargs.pop("framework_run_id", RUN),
        profile=kwargs.pop("profile", runtime.environment),
        allowed_units=kwargs.pop("allowed_units", quality()[1]), **kwargs,
    )


def command_rows(command):
    assert command.startswith(f".append {REPORT_TABLE} <|") or command.startswith(f".append {EVENT_TABLE} <|")
    return [json.loads(match) for match in re.findall(r"^dynamic\((.+)\),?$", command, re.MULTILINE)]


class FakeClient:
    def __init__(self):
        self.queries, self.commands, self.rows = [], [], []
        self.fail_queries = 0
        self.accept_count = None
        self.manage_error = None
        self.before_manage = None
        self.closed = False

    def query(self, statement):
        self.queries.append(statement)
        if self.fail_queries:
            self.fail_queries -= 1
            raise AdxUnavailable()
        column = "EventId" if statement.startswith(EVENT_TABLE) else "FrameworkRunId"
        ids = json.loads("[" + re.search(rf"\| where {column} in \((.+)\)", statement)[1] + "]")
        run = json.loads(re.search(r"FrameworkRunId == (.+)", statement)[1])
        return [{column: row[column], "ContentHash": row["ContentHash"]} for row in self.rows
                if column in row and row["FrameworkRunId"] == run and row[column] in ids
                and ("EventId" in row) == (column == "EventId")]

    def manage(self, statement):
        self.commands.append(statement)
        if self.before_manage is not None:
            self.before_manage(statement)
        rows = command_rows(statement)
        self.rows.extend(rows if self.accept_count is None else rows[:self.accept_count])
        if self.manage_error is not None:
            raise self.manage_error

    def close(self):
        self.closed = True


@pytest.mark.parametrize("excluded,status", [(0, "Full"), (1, "Partial"), (2, "Partial")])
def test_one_envelope_and_same_result_reach_adx_without_rescoring(tmp_path, excluded, status):
    result, plan = quality(excluded)
    envelope = build_public_report(result, allowed_units=plan, framework_run_id=RUN, **METADATA)
    assert set(envelope) == {"schema_version", "report_date", "source_commit", "region", "framework_run_id", "report"}
    assert envelope["report"] == result.to_dict() == public_projection(result, allowed_units=plan)
    assert envelope["report"]["status"] == status
    assert envelope["report"]["coverage"]["planned_issues"] == 3
    assert envelope["report"]["scoring_policy"]["noise_weight"] == 1
    assert envelope["report"]["scoring_policy"]["duplicate_weight"] == 0.25
    assert envelope["report"]["coverage"]["excluded_units"] == excluded
    copied = validate_public_report(envelope, allowed_units=plan)
    copied["report"]["counts"]["noise_cards"] = 999
    assert envelope["report"]["counts"]["noise_cards"] != 999
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        assert box.queue_report(result, **METADATA) == "report"
        assert box.read_request("report")["body"] == envelope
        assert not client.commands
        flushed = box.flush(client)
        assert (flushed.attempted, flushed.delivered, flushed.warnings) == (1, 1, ())
    row = client.rows[0]
    assert row["Payload"] == envelope["report"]
    assert row["SourceCommit"] == envelope["source_commit"]
    assert row["Region"] == envelope["region"]
    assert row["ReportDate"] == envelope["report_date"]
    assert row["FrameworkRunId"] == envelope["framework_run_id"]
    assert not client.closed


@pytest.mark.parametrize("metadata", [
    {"report_date": "2026-02-30"}, {"report_date": "20260904"}, {"report_date": "2026-09-04T00:00:00Z"},
    {"source_commit": "private-provider-id"}, {"source_commit": "https://synthetic.example.test"},
    {"region": "https://synthetic.example.test"}, {"region": "synthetic-private-location"},
    {"framework_run_id": "../escape"}, {"framework_run_id": "Provider_ID"}, {"framework_run_id": "a" * 65},
])
def test_metadata_rejects_unapproved_values(metadata):
    result, plan = quality()
    with pytest.raises(PublicationError):
        build_public_report(result, allowed_units=plan, **({"framework_run_id": RUN, **METADATA} | metadata))


def test_no_model_prose_raw_dtos_unknown_fields_or_guessed_plan(tmp_path):
    result, plan = quality()
    with pytest.raises(PrivacyError):
        build_public_report(result.to_dict(), allowed_units=plan, framework_run_id=RUN, **METADATA)
    unsafe = replace(result, units=(replace(result.units[0], summary="synthetic private prompt"), *result.units[1:]))
    with pytest.raises(PrivacyError):
        build_public_report(unsafe, allowed_units=plan, framework_run_id=RUN, **METADATA)
    envelope = build_public_report(result, allowed_units=plan, framework_run_id=RUN, **METADATA)
    for path in ("outer", "report", "unit", "finding"):
        changed = deepcopy(envelope)
        target = {
            "outer": changed, "report": changed["report"], "unit": changed["report"]["units"][0],
            "finding": changed["report"]["units"][0]["findings"][0],
        }[path]
        target["raw_spans"] = ["synthetic private span"]
        with pytest.raises((PrivacyError, PublicationError)):
            validate_public_report(changed, allowed_units=plan)
    with pytest.raises(PrivacyError):
        validate_public_report(envelope, allowed_units=plan[:-1])
    failed, _ = quality(excluded=3)
    with pytest.raises(PublicationError, match="ineligible"):
        build_public_report(failed, allowed_units=plan, framework_run_id=RUN, **METADATA)
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        with pytest.raises(PrivacyError):
            outbox(runtime).queue_report(unsafe, **METADATA)
    assert not list(tmp_path.rglob("*.json"))


def test_test_run_has_zero_client_store_repository_or_trend_side_effects(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    box = outbox(runtime, test_run=True)
    assert box.queue_report(quality()[0], **METADATA) is None
    assert box.queue_event(event()) is None
    assert box.read_request("report") is None
    assert box.flush(client).skipped
    assert asyncio.run(box.flush_async(client)).skipped
    assert not list(tmp_path.rglob("*"))
    assert not client.queries and not client.commands
    with runtime.ownership():
        official = outbox(runtime)
        official.queue_report(quality()[0], **METADATA)
        official.queue_event(event())
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert box.flush(client).skipped
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert not client.queries and not client.commands


def test_immutable_report_replay_and_local_content_conflict(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        result, _ = quality()
        box.queue_report(result, **METADATA)
        assert box.queue_report(result, **METADATA) == "report"
        assert box.flush(client).delivered == 1
        before = (len(client.commands), len(client.queries))
        assert outbox(runtime).flush(client).delivered == 1
        assert before == (len(client.commands), len(client.queries))
        with pytest.raises(StateConflict):
            box.queue_report(result, **(METADATA | {"source_commit": "b" * 40}))
        with pytest.raises(StateConflict):
            box.queue_report(quality(1)[0], **METADATA)
        with pytest.raises(StateConflict):
            outbox(runtime, allowed_units=quality()[1][:-1]).flush(client)


def test_event_allowlist_staging_and_logger_callback_are_independent(tmp_path):
    runtime, client = RuntimeStore("staging", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        with pytest.raises(PublicationError, match="not_daily"):
            box.queue_report(quality()[0], **METADATA)
        for bad in (
            event() | {"provider_id": "synthetic-provider"},
            event() | {"code": "synthetic private provider failure"},
            event() | {"counters": {"raw_payload": "synthetic"}},
            event() | {"agent": "not-in-plan", "logical_version": "v0"},
        ):
            with pytest.raises(ValueError):
                box.queue_event(bad)
        assert not list(tmp_path.rglob("*.json"))
        identity = box.queue_event(event())
        assert box.queue_event(event()) == identity
        assert box.read_request(identity)["body"]["event"] == project_event(event())
        with RunLogger(runtime.run(RUN).directory, outbox=box.queue_event, console=io.StringIO()) as logger:
            assert logger.emit("started", code="ok")
            assert not logger.health_warnings
        assert not client.queries
        assert box.flush(client).delivered == 2
    assert all(row["Profile"] == "staging" for row in client.rows)
    assert all(set(row) == {"FrameworkRunId", "Profile", "EventId", "ContentHash", "EventVersion", "Event"}
               for row in client.rows)
    assert len(client.commands) == 1


def test_environment_outbox_separation_and_allowed_event_unit(tmp_path):
    runtime, client = RuntimeStore("staging", root=tmp_path), FakeClient()
    with pytest.raises(PublicationError, match="store_not_outbox"):
        outbox(runtime, profile="daily")
    with pytest.raises(PublicationError, match="store_not_outbox"):
        PublicationOutbox(
            runtime.run(RUN), framework_run_id=RUN, profile="staging", allowed_units=quality()[1],
        )
    assert not list(tmp_path.rglob("*"))
    with runtime.ownership():
        box = outbox(runtime)
        projected = event() | {
            "agent": "synthetic-agent", "logical_version": "v0", "attempt": 3, "turn": 2,
        }
        identity = box.queue_event(projected)
        assert box.flush(client).delivered == 1
        assert client.rows[0]["Event"] == projected
        assert client.rows[0]["EventId"] == identity


def test_optional_outbox_persistence_failure_does_not_stop_local_logging(tmp_path, monkeypatch):
    runtime = RuntimeStore("staging", root=tmp_path)
    with runtime.ownership():
        box = outbox(runtime)

        def fail(*args):
            raise CheckpointError()

        monkeypatch.setattr(box.records, "save_completed", fail)
        directory = runtime.run(RUN).directory
        with RunLogger(directory, outbox=box.queue_event, console=io.StringIO(), stderr=io.StringIO()) as logger:
            assert logger.emit("started", code="ok")
            assert logger.health_warnings == ("logging_outbox_failed",)
        assert (directory / "runner.log").read_text(encoding="utf-8")
        assert json.loads((directory / "events.jsonl").read_text(encoding="utf-8"))["kind"] == "started"


def test_prequery_failure_leaves_a_durable_retry_without_requalification(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        outbox(runtime).queue_report(quality()[0], **METADATA)
        client.fail_queries = 1
        first = outbox(runtime).flush(client)
        assert (first.pending, first.unknown, first.warnings) == (1, 0, ("adx_delivery_failed",))
        assert not client.commands
        second = outbox(runtime).flush(client)
        assert (second.delivered, second.pending, second.warnings) == (1, 0, ())
    assert not (runtime.directory / "runs").exists()


def test_partial_batch_failure_reconciles_each_id_and_never_retries_unknown(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        ids = [box.queue_event(event(number)) for number in range(3)]
        client.accept_count, client.manage_error = 2, AdxUnavailable()
        result = box.flush(client)
        assert (result.attempted, result.delivered, result.unknown) == (3, 2, 1)
        assert result.warnings == ("adx_delivery_failed",)
        assert outbox(runtime).flush(client).unknown == 1
        assert len(client.commands) == 1
        submitted = command_rows(client.commands[0])
        client.rows.extend(submitted[2:])
        client.rows.append(submitted[0])  # Exact page/replay copy is not a conflict.
        final = outbox(runtime).flush(client)
        assert (final.delivered, final.unknown, final.conflicts) == (3, 0, ())
        assert len(client.commands) == 1
        assert all(box.read_request(identity) is not None for identity in ids)


def test_acceptance_without_visibility_is_unknown_but_definite_rejection_can_retry(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        box.queue_report(quality()[0], **METADATA)
        client.accept_count = 0
        client.manage_error = AdxUnavailable(request_accepted=False)
        assert box.flush(client).pending == 1
        client.manage_error = None
        assert box.flush(client).unknown == 1
        assert box.flush(client).unknown == 1
        assert len(client.commands) == 2
        client.rows.extend(command_rows(client.commands[-1]))
        assert box.flush(client).delivered == 1
        assert len(client.commands) == 2


def test_existing_ids_reconcile_and_conflicting_remote_content_is_explicit(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        box.queue_report(quality()[0], **METADATA)
        client.rows = [{"FrameworkRunId": RUN, "ContentHash": "b" * 64}]
        result = box.flush(client)
        assert result.conflicts == ("report",)
        assert result.warnings == ("adx_delivery_failed",)
        assert not client.commands
        other = outbox(runtime, framework_run_id="daily-2026-09-05-r0")
        other.queue_report(quality()[0], **METADATA)
        client.rows.append({"FrameworkRunId": other.framework_run_id,
                            "ContentHash": other.read_request("report")["content_hash"]})
        assert other.flush(client).delivered == 1
        assert not client.commands


def test_invalid_query_result_cannot_prove_absence_or_trigger_append(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        box.queue_report(quality()[0], **METADATA)
        client.rows = [{"FrameworkRunId": RUN, "ContentHash": "synthetic-invalid-hash"}]
        result = box.flush(client)
        assert (result.pending, result.unknown, result.warnings) == (1, 0, ("adx_delivery_failed",))
        assert not client.commands


def test_every_batch_member_checkpoint_precedes_manage_and_crash_after_acceptance_reconciles(tmp_path, monkeypatch):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        ids = [box.queue_event(event(number)) for number in range(2)]

        def check_unknown(_):
            for identity in ids:
                assert box.records.read(f"outcomes/{RUN}/{identity}")["state"] == "unknown"

        client.before_manage = check_unknown
        save = box.records.save_completed

        def fail_completion(key, value):
            if key.startswith("outcomes/"):
                raise CheckpointError()
            save(key, value)

        monkeypatch.setattr(box.records, "save_completed", fail_completion)
        with pytest.raises(CheckpointError):
            box.flush(client)
        assert len(client.commands) == 1
        assert outbox(runtime).flush(client).delivered == 2
        assert len(client.commands) == 1


def test_checkpoint_failure_stops_before_any_unsafe_append(tmp_path, monkeypatch):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        box.queue_report(quality()[0], **METADATA)

        def fail(*args):
            raise CheckpointError()

        monkeypatch.setattr(box.records, "save_progress", fail)
        with pytest.raises(CheckpointError):
            box.flush(client)
        assert not client.commands


def test_unsafe_persisted_request_rejected_before_network_even_with_recomputed_hash(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        box.queue_event(event())
        envelope = build_public_report(quality()[0], allowed_units=quality()[1], framework_run_id=RUN, **METADATA)
        envelope["report"]["units"][0]["summary"] = "synthetic private prompt"
        digest = hashlib.sha256(json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
        ).encode("ascii")).hexdigest()
        box.records.save_completed(f"requests/{RUN}/report", {"body": envelope, "content_hash": digest})
        with pytest.raises(PrivacyError):
            box.flush(client)
        assert not client.commands and not client.queries


def test_bounded_batches_rotate_past_unknown_requests_without_starvation(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        for number in range(5):
            box.queue_event(event(number))
        box.queue_report(quality()[0], **METADATA)
        client.accept_count = 0
        for _ in range(3):
            assert box.flush(client, batch_size=2, max_batches=1).attempted in (1, 2)
        last = box.flush(client, batch_size=2, max_batches=1)
        assert (last.pending, last.unknown) == (0, 6)
        assert len(client.commands) == 4
        for _ in range(4):
            box.flush(client, batch_size=2, max_batches=1)
        assert len(client.commands) == 4


def test_async_flush_does_not_block_loop_and_drains_worker_on_repeated_cancellation(tmp_path):
    runtime, client = RuntimeStore("daily", root=tmp_path), FakeClient()
    with runtime.ownership():
        box = outbox(runtime)
        box.queue_report(quality()[0], **METADATA)

        async def exercise():
            started, released = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()

            def block(_):
                loop.call_soon_threadsafe(started.set)
                assert released.wait(timeout=2)

            client.before_manage = block
            task = asyncio.create_task(box.flush_async(client))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            finally:
                released.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert box.flush(client).delivered == 1

        asyncio.run(exercise())
        assert len(client.commands) == 1


def test_sdk_is_lazy_missing_sdk_is_safe_and_unused_close_has_no_import(monkeypatch):
    original = builtins.__import__

    def no_sdk(name, *args, **kwargs):
        if name.startswith("azure"):
            raise ImportError("synthetic missing optional SDK")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    client = AzureCliAdxClient("https://synthetic.example.test", "SyntheticDatabase")
    with pytest.raises(AdxUnavailable) as failure:
        client.query("synthetic query")
    assert failure.value.request_accepted is False
    client.close()
    AzureCliAdxClient("https://synthetic.example.test", "SyntheticDatabase").close()


def test_lazy_sdk_adapter_uses_cli_bounded_strong_complete_queries_and_no_http_retries(monkeypatch):
    calls = []

    class Properties:
        def __init__(self):
            self.options = {}

        def set_option(self, name, value):
            self.options[name] = value

    class ServiceError(Exception):
        pass

    class Table(list):
        columns = [SimpleNamespace(column_name="ContentHash")]

    class Response:
        primary_results = [Table([{"ContentHash": "a" * 64}])]
        errors = []

        def get_exceptions(self):
            return self.errors

    response = Response()

    class SdkClient:
        def __init__(self, connection):
            calls.append(("connect", connection))

        def set_http_retries(self, retries):
            calls.append(("retries", retries))

        def execute_query(self, database, statement, properties):
            calls.append(("query", database, statement, properties.options))
            return response

        def execute_mgmt(self, database, statement, properties):
            calls.append(("manage", database, statement, properties.options))
            return response

        def close(self):
            calls.append(("close",))

    for name in ("azure", "azure.kusto", "azure.kusto.data", "azure.kusto.data.exceptions"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    data = sys.modules["azure.kusto.data"]
    data.ClientRequestProperties, data.KustoClient = Properties, SdkClient
    data.KustoConnectionStringBuilder = SimpleNamespace(
        with_az_cli_authentication=lambda uri: ("azure_cli", uri),
    )
    exceptions = sys.modules["azure.kusto.data.exceptions"]
    exceptions.KustoClientError = exceptions.KustoServiceError = ServiceError
    client = AzureCliAdxClient("https://synthetic.example.test", "SyntheticDatabase")
    assert calls == []
    assert client.query("synthetic query") == [{"ContentHash": "a" * 64}]
    assert calls[:2] == [("connect", ("azure_cli", "https://synthetic.example.test")), ("retries", 0)]
    assert calls[-1][-1] == {
        "servertimeout": "00:00:30", "norequesttimeout": False,
        "queryconsistency": "strongconsistency", "notruncation": True,
    }
    client.manage("synthetic management")
    assert calls[-1][0] == "manage"
    response.errors = ["synthetic private provider diagnostic"]
    with pytest.raises(AdxUnavailable, match="^adx_delivery_failed$"):
        client.query("synthetic query")
    client.close()
    assert calls[-1] == ("close",)


def test_versioned_kql_views_use_actual_result_paths_and_keep_legacy_history():
    root = Path(__file__).resolve().parents[2]
    kql = (root / "infra" / "quality-analytics.kql").read_text(encoding="utf-8")
    legacy, current = kql.split("// Current framework data", 1)
    assert ".create-merge table DailyQualityPublications" in legacy
    assert "PayloadVersion == '3.0.0'" in legacy
    assert "DailyQualityPublications" not in current
    assert "Payload.run" not in current and "BaselinePassed" not in current
    assert "arg_max(PublishedAt, *) by FrameworkRunId" in current
    assert "by FrameworkRunId, EventId" in current
    assert "array_length(ContentHashes) > 1" in current
    result = quality(1)[0].to_dict()
    fixtures = {"Payload": result, "Unit": result["units"][0], "Finding": result["units"][0]["findings"][0]}
    for alias, fixture in fixtures.items():
        for path in re.findall(rf"\b{alias}\.([a-z_]+(?:\.[a-z_]+)*)", current):
            value = fixture
            for field in path.split("."):
                value = value[field]
    assert "QualityScore = todouble(Payload.score)" in current
    assert "Payload.coverage.scored_baselines" in current
    assert "Payload.scoring_policy.duplicate_weight" in current
    assert "Scored = tobool(Finding.scored)" in current
    assert "FrameworkRunId:string" in current
    assert "EventId:string" in current
    assert "forceUpdateTag: 'quality-analytics-v7-public-outbox'" in (
        root / "infra" / "modules" / "quality-analytics.bicep"
    ).read_text(encoding="utf-8")


def test_current_dashboard_separates_coverage_policies_and_retains_legacy_pages():
    root = Path(__file__).resolve().parents[2]
    dashboard = json.loads((root / "dashboards" / "agent-insights-quality.template.json").read_text(encoding="utf-8"))
    current_page = dashboard["pages"][0]
    assert current_page["name"] == "Current framework"
    assert all(page["name"].startswith("Legacy") for page in dashboard["pages"][1:])
    assert dashboard["dataSources"][0]["clusterUri"] == "{{ADX_CLUSTER_URI}}"
    assert dashboard["dataSources"][0]["database"] == "{{ADX_DATABASE}}"
    tiles = [tile for tile in dashboard["tiles"] if tile["pageId"] == current_page["id"]]
    assert len(tiles) == 6
    assert all("AIQDaily" not in tile["query"] for tile in tiles)
    summary, trend, units, findings, operations, conflicts = tiles
    assert all(field in summary["query"] for field in (
        "CoverageStatus", "ScoringPolicy", "NoiseWeight", "DuplicateWeight", "ExcludedUnits",
        "ScoredIssues", "PlannedIssues", "ScoredBaselines", "PlannedBaselines",
    ))
    assert "CoverageStatus" in trend["query"] and "ScoringPolicy" in trend["query"]
    assert trend["visualOptions"]["seriesColumns"] == {"type": "specified", "value": ["Series"]}
    assert "ExclusionReasons" in units["query"]
    assert "Scored" in findings["query"]
    assert "AIQOperationsV1()" in operations["query"]
    assert "AIQPublicationConflictsV1()" in conflicts["query"]
