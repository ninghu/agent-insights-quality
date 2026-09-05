import asyncio
import hashlib
import json
from dataclasses import replace

import pytest

from agent_insights_quality.contracts import Target
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers import AcrImageBuilder, CommandResult
from agent_insights_quality.providers.artifacts import (
    prepare_artifact,
    source_files,
    source_zip,
)
from agent_insights_quality.results import UnitId


class Commands:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []
        self.contexts = []

    async def run(self, arguments, *, cwd=None):
        self.calls.append(list(arguments))
        if cwd:
            self.contexts.append(
                {
                    path.relative_to(cwd).as_posix(): path.read_bytes()
                    for path in cwd.rglob("*")
                    if path.is_file()
                }
            )
        assert self.values, "Unexpected command"
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, CommandResult):
            return value
        return CommandResult(0, json.dumps(value))


@pytest.fixture
def target(tmp_path):
    baseline = tmp_path / "baseline"
    root = tmp_path / "issue"
    (root / "source").mkdir(parents=True)
    (baseline / "source").mkdir(parents=True)
    (root / "source" / "app.py").write_text("selected = True\n")
    (root / "source" / "added.py").write_text("added = True\n")
    (baseline / "source" / "app.py").write_text("baseline = True\n")
    (baseline / "requirements.txt").write_text("synthetic-package==1.0\n")
    (baseline / "Dockerfile").write_text(
        "FROM python:3.12-slim\nCOPY v0/source /app/source\n"
        "COPY v0/implementation.yaml /app/issue.yaml\n"
    )
    (root / "implementation.yaml").write_text("issue_id: issue-029\n")
    return Target(
        UnitId("support-ticket-agent", "issue-029"),
        "hosted_custom_container",
        "deterministic",
        root,
        baseline,
        {},
    )


def test_container_context_uses_selected_complete_source_without_patching(target):
    files = source_files(target, container=True)
    assert files == {
        "v0/source/app.py": (target.version_root / "source" / "app.py").read_bytes(),
        "v0/source/added.py": (
            target.version_root / "source" / "added.py"
        ).read_bytes(),
        "v0/requirements.txt": (target.baseline_root / "requirements.txt").read_bytes(),
        "v0/Dockerfile": (target.baseline_root / "Dockerfile").read_bytes(),
        "v0/implementation.yaml": (
            target.version_root / "implementation.yaml"
        ).read_bytes(),
    }
    assert source_zip(files) == source_zip(dict(reversed(list(files.items()))))


def test_missing_selected_source_never_falls_back_to_baseline(target, tmp_path):
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(QualityError, match="deployment_source_missing"):
        source_files(replace(target, version_root=missing), container=True)


def test_scoped_acr_build_is_checkpointed_and_digest_pinned(target, tmp_path):
    files = source_files(target, container=True)
    key = hashlib.sha256(source_zip(files)).hexdigest()
    digest = "sha256:" + "a" * 64
    commands = Commands(
        [],
        {"runId": "native-run"},
        {"status": "Succeeded"},
        ["agent-insights-quality-support"],
        [{"name": "source-" + key, "digest": digest}],
    )
    saved = []
    builder = AcrImageBuilder(
        "exampleregistry",
        workspace=tmp_path / "builds",
        persist=saved.append,
        command=commands,
    )
    image = asyncio.run(builder.ensure_image(target, "r", files))
    assert (
        image == "exampleregistry.azurecr.io/agent-insights-quality-support@" + digest
    )
    assert [record["state"] for record in saved] == [
        "submitting",
        "pending",
        "completed",
    ]
    assert saved[1]["run_id"] == "native-run"
    builds = [command for command in commands.calls if command[:2] == ["acr", "build"]]
    assert len(builds) == 1
    assert "--no-wait" in builds[0] and "--no-logs" in builds[0]
    assert "login" not in " ".join(" ".join(command) for command in commands.calls)
    assert commands.contexts == [files]
    assert list((tmp_path / "builds").iterdir()) == []


def test_native_image_is_reused_across_environment_builders(target, tmp_path):
    files = source_files(target, container=True)
    key = hashlib.sha256(source_zip(files)).hexdigest()
    commands = Commands(
        ["agent-insights-quality-support"],
        [{"name": "source-" + key, "digest": "sha256:" + "b" * 64}],
    )
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=lambda x: None, command=commands
    )
    image = asyncio.run(builder.ensure_image(target, "new-unrelated-commit", files))
    assert image.endswith("@sha256:" + "b" * 64)
    assert not any(command[:2] == ["acr", "build"] for command in commands.calls)


def test_pending_native_build_resumes_without_uploading_again(target, tmp_path):
    files = source_files(target, container=True)
    commands = Commands([], {"runId": "native-run"}, {"status": "Running"})
    saved = []
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=saved.append, command=commands
    )
    with pytest.raises(QualityError, match="acr_build_pending"):
        asyncio.run(builder.ensure_image(target, "r", files))
    key = saved[-1]["artifact_key"]
    resumed_commands = Commands([], {"status": "Running"})
    resumed = AcrImageBuilder(
        "exampleregistry",
        workspace=tmp_path,
        persist=saved.append,
        records={key: saved[-1]},
        command=resumed_commands,
    )
    with pytest.raises(QualityError, match="acr_build_pending"):
        asyncio.run(resumed.ensure_image(target, "r", files))
    assert len(resumed_commands.calls) == 2
    assert resumed_commands.calls[-1][:4] == ["acr", "task", "show-run", "--run-id"]
    assert not resumed_commands.contexts


def test_ambiguous_build_is_not_retried_on_resume(target, tmp_path):
    files = source_files(target, container=True)
    saved = []
    commands = Commands(
        [], QualityError("acr_command_no_response", request_accepted=None)
    )
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=saved.append, command=commands
    )
    with pytest.raises(QualityError):
        asyncio.run(builder.ensure_image(target, "r", files))
    assert saved[-1]["state"] == "unknown"
    resumed = AcrImageBuilder(
        "exampleregistry",
        workspace=tmp_path,
        persist=saved.append,
        records={saved[-1]["artifact_key"]: saved[-1]},
        command=Commands([]),
    )
    with pytest.raises(QualityError, match="acr_build_unresolved"):
        asyncio.run(resumed.ensure_image(target, "r", files))


def test_failed_acr_lookup_never_triggers_a_build(target, tmp_path):
    commands = Commands(CommandResult(1, "synthetic private diagnostic"))
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=lambda x: None, command=commands
    )
    with pytest.raises(QualityError, match="acr_read_failed"):
        asyncio.run(
            builder.ensure_image(target, "r", source_files(target, container=True))
        )
    assert len(commands.calls) == 1


def test_artifact_checkpoint_failure_prevents_upload(target, tmp_path):
    commands = Commands([])

    def fail(value):
        raise OSError("synthetic disk failure")

    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=fail, command=commands
    )
    with pytest.raises(QualityError, match="provider_checkpoint_failed"):
        asyncio.run(
            builder.ensure_image(target, "r", source_files(target, container=True))
        )
    assert len(commands.calls) == 1


def test_custom_container_without_builder_is_explicit_capability_error(target):
    with pytest.raises(QualityError, match="container_builder_unavailable"):
        asyncio.run(
            prepare_artifact(target, None, "r", images=None, hosted_environment={})
        )
