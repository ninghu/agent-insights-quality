import copy
import hashlib
import json
import subprocess
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers import artifacts
from agent_insights_quality.providers.artifacts import prepare_artifact, source_zip
from agent_insights_quality.runner import deployment_revision
from test_provider_runtime import (
    FakeTransport, deployed, environment as environment, response, run, runtime, target as target,
)


class Images:
    def __init__(self):
        self.calls = []
        self.built = set()
        self.repository = "example.invalid/synthetic"

    async def ensure_image(self, target, revision, context):
        key = hashlib.sha256(source_zip(context)).hexdigest()
        self.calls.append((target.key, revision, key))
        self.built.add(key)
        return self.repository + "@sha256:" + key


@pytest.fixture(params=["prompt", "hosted_code", "hosted_custom_container"])
def selected(target, request):
    (target.version_root / "source").mkdir()
    (target.version_root / "source" / "app.py").write_text("synthetic = True\n")
    (target.baseline_root / "requirements.txt").write_text("synthetic-package==1.0\n")
    (target.baseline_root / "host.yaml").write_text("entrypoint: python -m source.app\n")
    (target.baseline_root / "Dockerfile").write_text(
        "FROM python:3.12-slim\nCOPY v0/source /app/source\n",
    )
    return replace(target, agent_type=request.param)


def native(deployment, **values):
    return {
        "version": deployment.provider_version, "status": "active",
        "metadata": dict(deployment.details["metadata"]), **values,
    }


def create(environment, target, *, images=None, variables=None):
    wire = FakeTransport(response(status=404), response({"version": "42", "status": "active"}, 201))
    saved = []
    result = run(runtime(
        environment, wire, images=images, hosted_environment=variables,
    ).ensure_deployment(target, "commit-one", None, saved.append))
    assert saved[0].content_hash == result.content_hash
    assert saved[0].details["metadata"]["aiq_content_hash"] == result.content_hash
    return result


def fingerprint(environment, target, *, images=None, variables=None):
    artifact = run(prepare_artifact(
        target, environment, "irrelevant-commit", images=images,
        hosted_environment=variables or {},
    ))
    return artifacts.deployment_content_hash(target, environment, artifact)


@pytest.mark.parametrize("registry_present", [True, False])
def test_identical_contents_reuse_native_version_and_original_provenance(
    environment, selected, registry_present,
):
    images = Images()
    original = create(environment, selected, images=images)
    before = copy.deepcopy(asdict(original))
    replies = [response(native(original))] if registry_present else [
        response({}), response({"data": [native(original)]}),
    ]
    wire = FakeTransport(*replies)
    observed = run(runtime(environment, wire, images=images).ensure_deployment(
        selected, "squash-merge-commit", original if registry_present else None, lambda value: None,
    ))
    assert observed.provider_version == original.provider_version == "42"
    assert observed.source_revision == "commit-one"
    assert observed.content_hash == original.content_hash
    assert asdict(original) == before
    assert all(request.method == "GET" for request in wire.requests)
    assert len(images.built) == (0 if selected.agent_type != "hosted_custom_container" else 1)


def test_changed_actual_inputs_create_a_new_content_keyed_version(environment, selected):
    images = Images()
    original = create(environment, selected, images=images)
    path = selected.version_root / ("definition.json" if selected.is_prompt else "source/app.py")
    if selected.is_prompt:
        value = json.loads(path.read_text())
        value["definition"]["model"] = "synthetic-new-model"
        path.write_text(json.dumps(value))
    else:
        path.write_text("synthetic = False\n")
    wire = FakeTransport(
        response({}), response({"data": [native(original)]}),
        response({"version": "43", "status": "active"}, 201),
    )
    result = run(runtime(environment, wire, images=images).ensure_deployment(
        selected, "commit-one", original, lambda value: None,
    ))
    assert result.provider_version == "43"
    assert result.source_revision == "commit-one"
    assert result.content_hash != original.content_hash
    assert [request.method for request in wire.requests] == ["GET", "GET", "POST"]


def test_traffic_evaluation_reporting_and_ignored_cache_files_are_not_inputs(environment, selected):
    images = Images()
    before = fingerprint(environment, selected, images=images)
    for filename in ("traffic.json", "implementation.yaml", "report.md"):
        (selected.version_root / filename).write_text("synthetic evaluation change")
    cache = selected.version_root / "source" / "__pycache__"
    cache.mkdir()
    (cache / "cached.pyc").write_bytes(b"synthetic cache")
    changed = replace(selected, expectation={"changed": True}, validation_mode="probability_tolerant")
    assert fingerprint(environment, changed, images=images) == before


@pytest.mark.parametrize("change", ["add", "delete", "rename", "requirements", "host", "dockerfile"])
@pytest.mark.parametrize("selected", ["hosted_code", "hosted_custom_container"], indirect=True)
def test_hosted_package_manifest_tracks_paths_dependencies_and_build_recipe(
    environment, selected, change,
):
    images = Images()
    extra = selected.version_root / "source" / "extra.py"
    extra.write_text("synthetic extra")
    before = fingerprint(environment, selected, images=images)
    if change == "add":
        (extra.parent / "added.py").write_text("synthetic added")
    elif change == "delete":
        extra.unlink()
    elif change == "rename":
        extra.rename(extra.parent / "renamed.py")
    else:
        name = {"requirements": "requirements.txt", "host": "host.yaml", "dockerfile": "Dockerfile"}[change]
        path = selected.baseline_root / name
        path.write_text(path.read_text() + "\n# synthetic dependency change\n")
    after = fingerprint(environment, selected, images=images)
    relevant = not (
        change == "host" and selected.agent_type == "hosted_custom_container"
        or change == "dockerfile" and selected.agent_type == "hosted_code"
    )
    assert (after != before) == relevant


def test_effective_environment_and_model_overrides_are_hashed_not_mapping_order(environment, selected):
    images = Images()
    variables = {"CUSTOM_SETTING": "one", "AZURE_AI_MODEL_DEPLOYMENT_NAME": "synthetic-model"}
    before = fingerprint(environment, selected, images=images, variables=variables)
    assert fingerprint(
        environment, selected, images=images, variables=dict(reversed(list(variables.items()))),
    ) == before
    for name in variables:
        after = fingerprint(
            environment, selected, images=images, variables={**variables, name: "changed"},
        )
        assert (after != before) == (not selected.is_prompt)
    for name in ("FOUNDRY_PROJECT_ENDPOINT", "AGENT_NAME", "APPLICATIONINSIGHTS_CONNECTION_STRING"):
        after = fingerprint(
            environment, selected, images=images, variables={**variables, name: "synthetic"},
        )
        assert (after != before) == (selected.agent_type == "hosted_code")


def test_resolved_hosted_project_placeholder_matches_literal(environment, selected):
    images = Images()
    assert fingerprint(environment, selected, images=images, variables={
        "CUSTOM_ENDPOINT": "${FOUNDRY_PROJECT_ENDPOINT}",
    }) == fingerprint(environment, selected, images=images, variables={
        "CUSTOM_ENDPOINT": environment.project_endpoint,
    })


def test_protocol_resource_and_runtime_definition_changes_affect_hosted_identity(
    environment, selected, monkeypatch,
):
    images = Images()
    before = fingerprint(environment, selected, images=images)
    original = artifacts.hosted_definition

    def definition(*args):
        value = original(*args)
        value.update(cpu="2", memory="4Gi", protocol_versions=[{"protocol": "responses", "version": "2"}])
        return value

    monkeypatch.setattr(artifacts, "hosted_definition", definition)
    after = fingerprint(environment, selected, images=images)
    assert (after != before) == (not selected.is_prompt)


def test_deployment_api_and_container_image_identity_are_inputs(environment, selected, monkeypatch):
    images = Images()
    before = fingerprint(environment, selected, images=images)
    images.repository = "another.invalid/another-repository"
    changed_image = fingerprint(environment, selected, images=images)
    assert (changed_image != before) == (selected.agent_type == "hosted_custom_container")
    monkeypatch.setattr(artifacts, "DEPLOYMENT_API_VERSION", "synthetic-v2")
    assert fingerprint(environment, selected, images=images) != changed_image


def test_content_identity_never_crosses_agent_logical_version_profile_or_project(environment, selected):
    images = Images()
    before = fingerprint(environment, selected, images=images)
    for unit in (
        replace(selected.unit_id, agent="another-agent"),
        replace(selected.unit_id, logical_version="issue-002"),
    ):
        assert fingerprint(environment, replace(selected, unit_id=unit), images=images) != before
    for changed in (
        replace(environment, profile="daily"),
        replace(environment, project_endpoint="https://another.invalid/api/projects/demo"),
    ):
        assert fingerprint(changed, selected, images=images) != before
    assert fingerprint(replace(
        environment, insights_endpoint="https://reporting.invalid",
        storage_account_name="reportstorage", region_display="another label",
    ), selected, images=images) == before


def test_legacy_registry_and_remote_metadata_are_not_adopted(environment, target):
    legacy = deployed(target, metadata={
        "aiq_profile": "staging", "aiq_logical_version": "issue-001",
        "aiq_source_revision": "revision-one",
    })
    before = copy.deepcopy(asdict(legacy))
    wire = FakeTransport(
        response({}), response({"data": [native(legacy)]}),
        response({"version": "43"}, 201),
    )
    result = run(runtime(environment, wire).ensure_deployment(
        target, "revision-one", legacy, lambda value: None,
    ))
    assert result.provider_version == "43" and result.content_hash is not None
    assert asdict(legacy) == before and legacy.content_hash is None
    assert all(request.method != "DELETE" for request in wire.requests)


@pytest.mark.parametrize("legacy", [False, True])
def test_frozen_known_deployment_never_repackages_or_replaces_after_source_change(
    environment, target, legacy,
):
    original = deployed(target) if legacy else create(environment, target)
    (target.version_root / "definition.json").unlink()
    wire = FakeTransport(response(
        {"version": "42"} if legacy else native(original),
    ))
    result = run(runtime(environment, wire).ensure_deployment(
        target, original.source_revision, original, lambda value: None, resume=True,
    ))
    assert result.provider_version == original.provider_version
    assert result.content_hash == original.content_hash
    assert result.source_revision == original.source_revision
    missing = FakeTransport(response(status=404))
    with pytest.raises(QualityError, match="deployment_frozen_version_missing"):
        run(runtime(environment, missing).ensure_deployment(
            target, original.source_revision, original, lambda value: None, resume=True,
        ))
    assert len(missing.requests) == 1


@pytest.mark.parametrize("changed_field", [
    "aiq_content_hash", "aiq_profile", "aiq_logical_version", "aiq_agent_type", "aiq_source_revision",
])
def test_exact_version_reuse_requires_complete_matching_native_metadata(environment, target, changed_field):
    original = create(environment, target)
    metadata = dict(original.details["metadata"])
    del metadata[changed_field]
    wire = FakeTransport(response(native(original, metadata=metadata)))
    with pytest.raises(QualityError, match="deployment_metadata_mismatch"):
        run(runtime(environment, wire).ensure_deployment(
            target, "new-commit", original, lambda value: None,
        ))
    assert len(wire.requests) == 1


def test_content_match_without_original_provenance_is_not_relabelled(environment, target):
    original = create(environment, target)
    metadata = dict(original.details["metadata"])
    del metadata["aiq_source_revision"]
    wire = FakeTransport(response({}), response({"data": [native(original, metadata=metadata)]}))
    with pytest.raises(QualityError, match="deployment_provenance_missing"):
        run(runtime(environment, wire).ensure_deployment(
            target, "new-commit", None, lambda value: None,
        ))
    assert all(request.method == "GET" for request in wire.requests)


@pytest.mark.parametrize("resume", [False, True])
def test_multiple_content_matches_remain_ambiguous(environment, target, resume):
    original = create(environment, target)
    pending = replace(original, provider_version="", details={
        **original.details, "provisioning_state": "unknown",
    })
    other = native(original, version="99", metadata={
        **original.details["metadata"],
        "aiq_source_revision": original.source_revision if resume else "another-commit",
    })
    wire = FakeTransport(response({}), response({"data": [native(original), other]}))
    with pytest.raises(QualityError, match="deployment_versions_ambiguous"):
        run(runtime(environment, wire).ensure_deployment(
            target, original.source_revision if resume else "new-commit",
            pending if resume else None, lambda value: None, resume=resume,
        ))
    assert all(request.method == "GET" for request in wire.requests)


@pytest.mark.parametrize("recover", [False, True])
def test_unknown_submission_reconciles_original_identity_without_repackaging(
    environment, target, recover,
):
    saved = []
    wire = FakeTransport(response(status=404), TimeoutError("synthetic"))
    with pytest.raises(QualityError, match="provider_no_response"):
        run(runtime(environment, wire).ensure_deployment(target, "commit-one", None, saved.append))
    pending = saved[-1]
    assert pending.content_hash and pending.details["provisioning_state"] == "unknown"
    (target.version_root / "definition.json").unlink()
    found = native(pending, version="42")
    # Same content under another commit cannot resolve this exact ambiguous submission.
    other = native(pending, version="99", metadata={
        **pending.details["metadata"], "aiq_source_revision": "another-commit",
    })
    wire = FakeTransport(response({}), response({"data": [other, *([found] if recover else [])]}))
    operation = runtime(environment, wire).ensure_deployment(
        target, "commit-one", pending, saved.append, resume=True,
    )
    if recover:
        result = run(operation)
        assert result.provider_version == "42" and result.source_revision == "commit-one"
        assert result.content_hash == pending.content_hash
    else:
        with pytest.raises(QualityError, match="deployment_create_unresolved"):
            run(operation)
    assert all(request.method == "GET" for request in wire.requests)


def test_known_rejected_retry_refuses_different_content(environment, target):
    saved = []
    wire = FakeTransport(response(status=404), response({}, 400))
    with pytest.raises(QualityError):
        run(runtime(environment, wire).ensure_deployment(target, "commit-one", None, saved.append))
    pending = saved[-1]
    assert pending.details["provisioning_state"] == "rejected"
    path = target.version_root / "definition.json"
    value = json.loads(path.read_text())
    value["definition"]["instructions"] = "Changed"
    path.write_text(json.dumps(value))
    wire = FakeTransport(response(status=404))
    with pytest.raises(QualityError, match="deployment_content_changed"):
        run(runtime(environment, wire).ensure_deployment(
            target, "commit-one", pending, saved.append, resume=True,
        ))
    assert all(request.method == "GET" for request in wire.requests)


@pytest.mark.parametrize("change", ["unchanged", "source", "environment"])
@pytest.mark.parametrize("selected", ["hosted_custom_container"], indirect=True)
def test_rejected_container_validates_inputs_before_any_build(environment, selected, change):
    images = Images()
    wire = FakeTransport(response(status=404), response({}, 400))
    saved = []
    with pytest.raises(QualityError):
        run(runtime(environment, wire, images=images).ensure_deployment(
            selected, "commit-one", None, saved.append,
        ))
    assert len(images.calls) == 1
    variables = {}
    if change == "source":
        (selected.version_root / "source" / "app.py").write_text("changed = True\n")
    elif change == "environment":
        variables = {"AZURE_AI_MODEL_DEPLOYMENT_NAME": "synthetic-new-model"}
    wire = FakeTransport(response(status=404), *(
        [response({"version": "42", "status": "active"}, 201)] if change == "unchanged" else []
    ))
    operation = runtime(
        environment, wire, images=images, hosted_environment=variables,
    ).ensure_deployment(selected, "commit-one", saved[-1], saved.append, resume=True)
    if change == "unchanged":
        result = run(operation)
        assert result.provider_version == "42"
        assert len(images.calls) == 2 and len(images.built) == 1
    else:
        with pytest.raises(QualityError, match="deployment_content_changed"):
            run(operation)
        assert len(images.calls) == 1
        assert all(request.method == "GET" for request in wire.requests)


@pytest.mark.parametrize("legacy", [False, True])
def test_unresolved_registry_record_blocks_new_work_before_side_effects(environment, target, legacy):
    original = deployed(target) if legacy else create(environment, target)
    pending = replace(original, details={**original.details, "provisioning_state": "unknown"})
    wire = FakeTransport()
    with pytest.raises(QualityError, match="deployment_create_unresolved"):
        run(runtime(environment, wire).ensure_deployment(
            target, "another-commit", pending, lambda value: None,
        ))
    assert not wire.requests


def test_real_squash_history_changes_provenance_but_not_deployment_identity(environment, target, tmp_path):
    root = tmp_path / "repo"
    selected = replace(
        target, version_root=root / "agents" / "example-agent" / "issues" / "issue-001",
        baseline_root=root / "agents" / "example-agent" / "v0",
    )
    selected.version_root.mkdir(parents=True)
    path = selected.version_root / "definition.json"
    path.write_bytes((target.version_root / "definition.json").read_bytes())

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()

    git("init", "--quiet", "--initial-branch=main")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("add", "--all")
    git("commit", "--quiet", "-m", "Initial synthetic asset")
    git("checkout", "--quiet", "-b", "candidate")
    value = json.loads(path.read_text())
    value["definition"]["instructions"] = "Reviewed candidate"
    path.write_text(json.dumps(value))
    git("add", "--all")
    git("commit", "--quiet", "-m", "Reviewed candidate")
    catalog = SimpleNamespace(root=root)
    branch_revision = deployment_revision(catalog, selected)
    saved = []
    wire = FakeTransport(response(status=404), response({"version": "42"}, 201))
    original = run(runtime(environment, wire).ensure_deployment(
        selected, branch_revision, None, saved.append,
    ))
    git("checkout", "--quiet", "main")
    git("merge", "--squash", "candidate")
    git("commit", "--quiet", "-m", "Squashed candidate")
    main_revision = deployment_revision(catalog, selected)
    assert branch_revision != main_revision
    assert git("rev-parse", "HEAD^{tree}") == git("rev-parse", "candidate^{tree}")
    wire = FakeTransport(response(native(original)))
    result = run(runtime(environment, wire).ensure_deployment(
        selected, main_revision, original, saved.append,
    ))
    assert result.provider_version == "42"
    assert result.content_hash == original.content_hash
    assert result.source_revision == branch_revision
    assert [request.method for request in wire.requests] == ["GET"]
