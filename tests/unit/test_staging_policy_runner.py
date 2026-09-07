"""Normal staging selection can reaggregate policy-only changes without traffic."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_insights_quality import assessment, runner as module
from agent_insights_quality.assessment import AssessmentError
from agent_insights_quality.selection import SourceChanges
from agent_insights_quality.staging_policy import STAGING_POLICY, StagingPolicy, StagingPolicyMigration
import test_runner as fake
from test_assessment import legacy_staging_result

POLICY_PATH = "src/agent_insights_quality/staging_policy.py"
PRIOR_POLICY = StagingPolicy("synthetic-six-v1", 6)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    fake.fake_storage(monkeypatch)


def previous_run(tmp_path, monkeypatch, *, ready=6, observations=None, source_revision="prior-source"):
    h = fake.Harness(tmp_path, profile="staging", issues=0)
    h.cloud.ready_count = ready
    complete = h.sol.complete_json
    async def judge(**kwargs):
        value = await complete(**kwargs)
        if observations is not None:
            for item in value["attempts"][observations:]:
                item["observed"] = False
        return value
    h.sol.complete_json = judge
    current = assessment.assess_staging
    async def old_policy(*args, **kwargs):
        result = await current(*args, **kwargs, policy=PRIOR_POLICY)
        value = legacy_staging_result(result.to_private_dict())
        return SimpleNamespace(to_private_dict=lambda: deepcopy(value))
    with monkeypatch.context() as old:
        old.setattr(assessment, "assess_staging", old_policy)
        old.setattr(module, "STAGING_POLICY", PRIOR_POLICY)
        result = h.staging("six-policy", revision=source_revision)
    return h, result["results"][0]


@pytest.mark.parametrize("ready,observations,status,count", [
    (6, None, "INCOMPLETE", 6),
    (10, 7, "FAIL", 7),
    (8, None, "INCOMPLETE", 8),
])
def test_normal_selection_to_runner_reclassifies_retained_six_policy_without_new_calls(
    tmp_path, monkeypatch, ready, observations, status, count,
):
    h, previous = previous_run(tmp_path, monkeypatch, ready=ready, observations=observations)
    target = h.catalog.targets[0]
    assert previous["status"] == "PASS" and previous["minimum_required"] == 6
    before = {path: path.read_bytes() for path in h.store.run("six-policy").directory.rglob("*.json")}
    old_artifact = h.store.run(previous["assessment"]["run_id"]).read_artifact(previous["assessment"]["artifact"])
    calls = len(h.cloud.events), len(h.sol.calls)
    selections = module.choose_staging(
        h.catalog, h.store, changes_since=lambda _: SourceChanges((POLICY_PATH,)),
    )
    assert len(selections) == 1 and selections[0].action == "reassess"
    assert "staging_policy_changed" in selections[0].reasons
    result = h.staging("eight-policy", selections=selections, revision="current-source", changes=(POLICY_PATH,))
    current = result["results"][0]
    assert current["status"] == status and current["result"]["passing_attempts"] == count
    assert current["minimum_required"] == current["result"]["minimum_required"] == 8
    assert current["policy_version"] == current["result"]["policy_version"] == STAGING_POLICY.version
    assert result["staging_policy"] == STAGING_POLICY.to_dict()
    assert current["source_revision"] == "current-source"
    assert current["traffic_source_revision"] == current["judgment_source_revision"] == "prior-source"
    assert current["evidence_key"] == previous["evidence_key"]
    assert current["result"]["root_hygiene_status"] == "NOT_EVALUATED"
    assert current["result"]["additional_findings"] is None
    assert "legacy_root_hygiene_not_evaluated" in current["result"]["reasons"]
    assert (len(h.cloud.events), len(h.sol.calls)) == calls
    assert {path: path.read_bytes() for path in before} == before
    new_artifact = h.store.run("eight-policy").read_artifact(current["assessment"]["artifact"])
    assert new_artifact["judgments"] == old_artifact["judgments"]
    assert new_artifact["private_detail"]["input"] == old_artifact["private_detail"]["input"]
    assert new_artifact["private_detail"]["output"] == old_artifact["private_detail"]["output"]
    origin = new_artifact["private_detail"]["policy_reassessment"]
    assert origin["run_id"] == previous["assessment"]["run_id"]
    assert origin["artifact"] == previous["assessment"]["artifact"]
    assert origin["previous_minimum_required"] == 6
    assert origin["judgment_source_revision"] == "prior-source"
    assert h.store.staging_index.read(target.key)["policy_version"] == STAGING_POLICY.version
    repeated = h.staging("eight-policy", selections=selections, revision="current-source", changes=(POLICY_PATH,))
    assert repeated["results"][0]["assessment"] == current["assessment"]
    assert (len(h.cloud.events), len(h.sol.calls)) == calls


@pytest.mark.parametrize("semantic_path", [
    "src/agent_insights_quality/assessment.py",
    "src/agent_insights_quality/prompts/staging.md",
    "src/agent_insights_quality/providers/sol.py",
])
def test_semantic_changes_still_reassess_with_model_not_cached_judgments(tmp_path, monkeypatch, semantic_path):
    h, previous = previous_run(tmp_path, monkeypatch, ready=10)
    changes = (POLICY_PATH, semantic_path)
    selections = module.choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(changes))
    assert selections[0].action == "reassess"
    calls, sol_calls = len(h.cloud.events), len(h.sol.calls)
    result = h.staging("semantic-change", selections=selections, revision="new-source", changes=changes)
    assert len(h.sol.calls) == sol_calls + 1
    assert all(event[0] == "assessment" for event in h.cloud.events[calls:])
    assert result["results"][0]["judgment_source_revision"] == "new-source"
    assert result["results"][0]["evidence_key"] == previous["evidence_key"]
    assert "policy_reassessment" not in result["results"][0]


def test_policy_only_reuse_rejects_invalid_retained_citations_without_new_sol(tmp_path, monkeypatch):
    h, previous = previous_run(tmp_path, monkeypatch)
    artifact_path = h.store.run("six-policy")._path("artifacts", previous["assessment"]["artifact"])
    import json
    value = json.loads(artifact_path.read_text())
    value["private_detail"]["output"]["attempts"][0]["citations"][0]["refs"] = ["not-a-valid-ref"]
    artifact_path.write_text(json.dumps(value))
    selections = module.choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges((POLICY_PATH,)))
    count = len(h.sol.calls), len(h.cloud.events)
    result = h.staging("reject", selections=selections, revision="new-source", changes=(POLICY_PATH,))
    assert result["results"][0]["status"] == "INCOMPLETE"
    assert result["results"][0]["reason"] == "assessment_citation_invalid"
    assert (len(h.sol.calls), len(h.cloud.events)) == count
    assert h.store.run("six-policy").read_artifact(previous["assessment"]["artifact"]) == value


def test_policy_module_is_evaluation_only_and_no_runtime_threshold_override_exists(tmp_path):
    from agent_insights_quality.selection import deployment_inputs, evaluation_inputs, traffic_inputs
    from agent_insights_quality.settings import RuntimeSettings
    h = fake.Harness(tmp_path, profile="staging")
    for target in h.catalog.targets:
        policy = h.catalog.root / POLICY_PATH
        assert policy in evaluation_inputs(target)
        assert policy not in deployment_inputs(target) and policy not in traffic_inputs(target)
    assert "minimum_required" not in RuntimeSettings().to_dict()
    assert RuntimeSettings().readiness_attempts == 6


def test_pure_reassessment_artifact_survives_crash_before_result_without_repeated_sol(tmp_path, monkeypatch):
    h, _ = previous_run(tmp_path, monkeypatch)
    selections = module.choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges((POLICY_PATH,)))
    original = module.Runner._update
    crashed = False
    def interrupt(self, target, work, **fields):
        nonlocal crashed
        if "result" in fields and not crashed:
            crashed = True
            raise OSError("synthetic interrupted pure policy result")
        return original(self, target, work, **fields)
    monkeypatch.setattr(module.Runner, "_update", interrupt)
    calls = len(h.sol.calls), len(h.cloud.events)
    with pytest.raises(OSError):
        h.staging("policy", selections=selections, revision="current-source", changes=(POLICY_PATH,))
    checkpoint = deepcopy(h.store.run("policy").read(f"targets/{h.catalog.targets[0].key}/assessment"))
    def no_reaggregate(*args, **kwargs):
        raise AssessmentError("should_not_reaggregate_completed_artifact")
    monkeypatch.setattr(assessment, "reassess_staging_policy", no_reaggregate)
    result = h.staging("policy", selections=selections, revision="current-source", changes=(POLICY_PATH,))
    assert result["results"][0]["assessment"]["artifact"] == checkpoint["artifact"]
    assert result["results"][0]["status"] == "INCOMPLETE"
    assert (len(h.sol.calls), len(h.cloud.events)) == calls


def test_explicit_reviewed_migration_reuses_source_bound_judgments_across_initial_refactor(tmp_path, monkeypatch):
    source = "a" * 40
    h, previous = previous_run(tmp_path, monkeypatch, source_revision=source)
    migration = StagingPolicyMigration(source, STAGING_POLICY)
    changes = (POLICY_PATH, "src/agent_insights_quality/assessment.py", "src/agent_insights_quality/prompts/staging.md")
    selected = module.choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(changes))
    assert selected[0].action == "reassess"
    before = len(h.sol.calls), len(h.cloud.events)
    original = h.store.run("six-policy").read_artifact(previous["assessment"]["artifact"])
    result = h.staging(
        "initial-migration", selections=selected, revision="b" * 40, changes=changes,
        staging_policy_migration=migration,
    )
    current = result["results"][0]
    assert current["status"] == "INCOMPLETE" and current["minimum_required"] == 8
    assert current["judgment_source_revision"] == current["traffic_source_revision"] == source
    assert (len(h.sol.calls), len(h.cloud.events)) == before
    assert h.store.run("initial-migration").read_completed("staging-policy-migration") == migration.to_dict()
    artifact = h.store.run("initial-migration").read_artifact(current["assessment"]["artifact"])
    assert artifact["private_detail"]["policy_reassessment"]["migration"] == migration.to_dict()
    assert h.store.run("six-policy").read_artifact(previous["assessment"]["artifact"]) == original
    from agent_insights_quality.errors import QualityError
    with pytest.raises(QualityError, match="staging_policy_migration_mismatch"):
        h.staging("initial-migration", selections=selected, revision="b" * 40, changes=changes)
    repeated = h.staging(
        "initial-migration", selections=selected, revision="b" * 40, changes=changes,
        staging_policy_migration=migration,
    )
    assert repeated["results"][0]["assessment"] == current["assessment"]
    assert (len(h.sol.calls), len(h.cloud.events)) == before


@pytest.mark.parametrize("change", ["source-mismatch", "traffic", "collector"])
def test_explicit_migration_never_reuses_changed_execution_source_or_collector_input(tmp_path, monkeypatch, change):
    source = "a" * 40
    h, previous = previous_run(tmp_path, monkeypatch, ready=10, source_revision=source)
    extra = {
        "source-mismatch": "src/agent_insights_quality/assessment.py",
        "traffic": h.catalog.targets[0].version_root.relative_to(h.catalog.root) / "traffic.json",
        "collector": "src/agent_insights_quality/telemetry.py",
    }[change]
    migration = StagingPolicyMigration("c" * 40 if change == "source-mismatch" else source, STAGING_POLICY)
    changes = (POLICY_PATH, extra)
    selected = module.choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(changes))
    calls = len(h.sol.calls), len(h.cloud.invocations), sum(item[0] == "query" for item in h.cloud.events)
    result = h.staging(
        "not-pure", selections=selected, revision="b" * 40, changes=changes, staging_policy_migration=migration,
    )
    current = result["results"][0]
    assert len(h.sol.calls) == calls[0] + 1
    assert current["status"] == "PASS" and current["minimum_required"] == 8
    assert current["judgment_source_revision"] == "b" * 40
    assert "policy_reassessment" not in current
    if change == "traffic":
        assert len(h.cloud.invocations) == calls[1] + 20
        assert current["traffic_source_revision"] == "b" * 40
    else:
        assert len(h.cloud.invocations) == calls[1]
        assert current["traffic_source_revision"] == source
    if change == "collector":
        assert sum(item[0] == "query" for item in h.cloud.events) > calls[2]
        assert current["evidence_key"] != previous["evidence_key"]


def test_explicit_migration_missing_judgment_uses_normal_assessment(tmp_path):
    h = fake.Harness(tmp_path, profile="staging", issues=0)
    h.sol.fail = True
    h.staging("unjudged", revision="a" * 40)
    h.sol.fail = False
    changes = (POLICY_PATH, "src/agent_insights_quality/assessment.py")
    selected = module.choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(changes))
    calls = len(h.sol.calls), len(h.cloud.invocations)
    result = h.staging(
        "migration", selections=selected, revision="b" * 40, changes=changes,
        staging_policy_migration=StagingPolicyMigration("a" * 40, STAGING_POLICY),
    )
    current = result["results"][0]
    assert current["status"] == "PASS" and current["minimum_required"] == 8
    assert len(h.sol.calls) == calls[0] + 1 and len(h.cloud.invocations) == calls[1]
    assert "policy_reassessment" not in current


def test_migration_destination_must_match_current_policy_and_is_staging_only(tmp_path):
    from agent_insights_quality.errors import QualityError
    h = fake.Harness(tmp_path, profile="staging")
    with pytest.raises(QualityError, match="staging_policy_migration_invalid"):
        h.runner(staging_policy_migration=StagingPolicyMigration("a" * 40, PRIOR_POLICY))
    daily = fake.Harness(tmp_path / "daily")
    with pytest.raises(QualityError, match="staging_policy_migration_invalid"):
        daily.runner(staging_policy_migration=StagingPolicyMigration("a" * 40, STAGING_POLICY))
    assert not h.cloud.events and not daily.cloud.events


def test_explicit_migration_rejects_drifted_expectations_and_does_not_call_sol(tmp_path, monkeypatch):
    from dataclasses import replace
    h, _ = previous_run(tmp_path, monkeypatch, source_revision="a" * 40)
    target = h.catalog.targets[0]
    h.catalog = replace(h.catalog, targets=(
        replace(target, expectation={"root_cause": "Changed reviewed meaning"}),
    ))
    changes = (POLICY_PATH, "catalogs/AGENT_CATALOG.yaml")
    selected = module.choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(changes))
    counts = len(h.sol.calls), len(h.cloud.events)
    result = h.staging(
        "rejected-migration", selections=selected, revision="b" * 40, changes=changes,
        staging_policy_migration=StagingPolicyMigration("a" * 40, STAGING_POLICY),
    )
    current = result["results"][0]
    assert current["status"] == "INCOMPLETE" and current["reason"] == "assessment_policy_input_mismatch"
    assert current["minimum_required"] == 8 and current["policy_version"] == STAGING_POLICY.version
    assert (len(h.sol.calls), len(h.cloud.events)) == counts


def test_initial_migration_hook_is_python_only_and_normal_cli_routes_real_selection(tmp_path, monkeypatch, capsys):
    from contextlib import asynccontextmanager
    from agent_insights_quality import catalogs, cli
    source = "a" * 40
    h, _ = previous_run(tmp_path, monkeypatch, source_revision=source)
    changes = (POLICY_PATH, "src/agent_insights_quality/assessment.py")
    monkeypatch.setattr(catalogs, "load_catalog", lambda _: h.catalog)
    monkeypatch.setattr(cli, "_committed_inputs", lambda _: None)
    monkeypatch.setattr(module, "source_revision", lambda _: "b" * 40)
    monkeypatch.setattr(module, "git_source_changes", lambda *args: SourceChanges(changes))
    runner = module.Runner
    def create(*args, **kwargs):
        return runner(*args, **kwargs, attempts=fake.attempts, now=h.clock.now,
                      sleep=h.clock.sleep, monotonic=h.clock.monotonic,
                      deployment_source=lambda _: "deployment-one", changes_since=lambda _: SourceChanges(changes))
    monkeypatch.setattr(module, "Runner", create)
    @asynccontextmanager
    async def ports(*args):
        yield h.cloud, h.sol, h.registry
    count = len(h.sol.calls), len(h.cloud.events)
    result = cli.main(
        ["run-staging"], root=h.catalog.root, runtime_factory=lambda _: h.store,
        ports=ports, today=fake.DAY, staging_policy_migration=StagingPolicyMigration(source, STAGING_POLICY),
    )
    assert result == 2
    assert (len(h.sol.calls), len(h.cloud.events)) == count
    output = capsys.readouterr().out
    import json
    summary = json.loads(output.splitlines()[-1])
    private = h.store.run(summary["run_id"]).read("staging-result")
    assert private["staging_policy"] == STAGING_POLICY.to_dict()
    assert private["results"][0]["minimum_required"] == 8
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["run-staging", "--staging-policy-migration", source])


def test_whole_staging_unit_exposes_private_hygiene_and_restores_without_model_calls(tmp_path):
    from test_staging_root_hygiene import finding
    h = fake.Harness(tmp_path, profile="staging", issues=0)
    complete = h.sol.complete_json
    async def judge(**kwargs):
        value = await complete(**kwargs)
        value["additional_findings"] = [finding(kwargs["payload"], index=10)]
        return value
    h.sol.complete_json = judge
    result = h.staging()
    current = result["results"][0]
    assert current["status"] == current["result"]["root_hygiene_status"] == "FAIL"
    assert current["result"]["passing_attempts"] == 10
    assert current["result"]["root_hygiene_reasons"] == ["proven_additional_agent_defect"]
    artifact = h.store.run("stage").read_artifact(current["assessment"]["artifact"])
    assert current["result"]["additional_findings"] == artifact["additional_findings"]
    assert artifact["private_detail"]["partitions"][0]["output"]["additional_findings"] == artifact["additional_findings"]
    counts = len(h.sol.calls), len(h.cloud.events)
    assert h.staging()["results"][0]["result"] == {key: artifact[key] for key in current["result"]}
    assert (len(h.sol.calls), len(h.cloud.events)) == counts


def test_reading_legacy_history_preserves_artifact_status_and_policy_without_new_review(tmp_path, monkeypatch):
    h, previous = previous_run(tmp_path, monkeypatch, ready=10)
    old = h.store.run("six-policy")
    before = {path: path.read_bytes() for path in old.directory.rglob("*.json")}
    counts = len(h.sol.calls), len(h.cloud.events)
    result = h.staging("historical-view", revision="prior-source")
    current = result["results"][0]
    assert current["status"] == previous["status"] == "PASS"
    assert current["policy_version"] == previous["policy_version"] == PRIOR_POLICY.version
    assert current["minimum_required"] == 6
    assert current["result"]["root_hygiene_status"] == "NOT_EVALUATED"
    assert current["result"]["additional_findings"] is None
    assert current["tested_at"] == previous["tested_at"]
    assert {path: path.read_bytes() for path in before} == before
    assert (len(h.sol.calls), len(h.cloud.events)) == counts
