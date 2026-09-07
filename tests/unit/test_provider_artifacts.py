import asyncio
import ast
import hashlib
import json
import shlex
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from agent_insights_quality.contracts import Target
from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers import AcrImageBuilder, AzureCommandRunner, CommandResult
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
    )
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
    }
    assert source_zip(files) == source_zip(dict(reversed(list(files.items()))))


def test_manifest_only_edits_do_not_change_support_image_contents(target):
    before = source_zip(source_files(target, container=True))
    (target.version_root / "implementation.yaml").write_text("expected_behavior: synthetic revision\n")
    after = source_zip(source_files(target, container=True))
    assert before == after


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
        {"runId": "native-run", "status": "Succeeded"},
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
    assert "--no-wait" not in builds[0] and "--no-logs" in builds[0]
    assert "login" not in " ".join(" ".join(command) for command in commands.calls)
    assert commands.contexts == [files]
    context_path = Path(saved[0]["context_path"])
    assert list((tmp_path / "builds").iterdir()) == [context_path]
    assert (context_path / "v0" / "source" / "app.py").read_bytes() == files["v0/source/app.py"]
    assert saved[-1]["context_path"] == str(context_path)


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
    commands = Commands([], {"runId": "native-run"}, {"runId": "native-run", "status": "Running"})
    saved = []
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=saved.append, command=commands
    )
    with pytest.raises(QualityError, match="acr_build_pending"):
        asyncio.run(builder.ensure_image(target, "r", files))
    key = saved[-1]["artifact_key"]
    resumed_commands = Commands([], {"runId": "native-run", "status": "Running"})
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


@pytest.mark.parametrize("version", [
    target.unit_id.logical_version
    for target in load_catalog(Path(__file__).resolve().parents[2]).for_agent("support-ticket-agent")
])
def test_real_support_sources_satisfy_selected_acr_build_context(version, tmp_path):
    root = Path(__file__).resolve().parents[2]
    target = load_catalog(root).target("support-ticket-agent/" + version)
    context = source_files(target, container=True)
    copies = {}
    dockerfile = context["v0/Dockerfile"].decode("utf-8")
    for line in dockerfile.splitlines():
        if line.startswith("COPY "):
            source, destination = shlex.split(line)[1:]
            assert source in context or any(name.startswith(source + "/") for name in context)
            copies[destination] = source
    assert copies == {
        "/app/requirements.txt": "v0/requirements.txt",
        "/app/source": "v0/source",
    }
    assert "-r /app/requirements.txt" in dockerfile
    assert 'CMD ["python", "-m", "source.app"]' in dockerfile
    assert "v0/implementation.yaml" not in context
    assert "ISSUE_PATH" not in dockerfile and "issue.yaml" not in dockerfile
    expected_sources = {
        "v0/source/" + path.relative_to(target.version_root / "source").as_posix(): path.read_bytes()
        for path in (target.version_root / "source").rglob("*.py")
    }
    assert {name: data for name, data in context.items() if name.startswith("v0/source/")} == expected_sources
    for source in expected_sources.values():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                assert "v0/source/" + node.module.replace(".", "/") + ".py" in context
    assert not any(name.startswith("issues/") for name in context)
    commands = Commands([], {"runId": "synthetic-native-run"}, {
        "runId": "synthetic-native-run", "status": "Running",
    })
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=lambda value: None, command=commands,
    )
    with pytest.raises(QualityError, match="acr_build_pending"):
        asyncio.run(builder.ensure_image(target, "synthetic-source", context))
    assert commands.contexts == [context]
    command = next(call for call in commands.calls if call[:2] == ["acr", "build"])
    assert command[command.index("--file") + 1] == "v0/Dockerfile"
    assert "--build-arg" not in command


@pytest.mark.parametrize("stdout", ["", "not json", "Queued a build with ID: native-run", "null", "{}"])
def test_invalid_submission_output_retains_context_and_never_runs_cleanup(
    target, tmp_path, monkeypatch, stdout,
):
    import agent_insights_quality.providers.acr as acr

    def forbidden(*args, **kwargs):
        raise PermissionError("Synthetic Windows cleanup failure")

    monkeypatch.setattr(acr.tempfile, "TemporaryDirectory", forbidden)
    saved = []
    commands = Commands([], CommandResult(0, stdout))
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path / "private-builds", persist=saved.append, command=commands,
    )
    context = source_files(target, container=True)
    with pytest.raises(QualityError, match="acr_build_identity_missing"):
        asyncio.run(builder.ensure_image(target, "r", context))
    assert saved[-1]["state"] == "unknown"
    root = Path(saved[-1]["context_path"])
    assert root.parent == tmp_path / "private-builds"
    assert {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()} == context
    assert len(commands.calls) == 2


def test_native_arm_run_properties_are_supported_without_console_parsing(target, tmp_path):
    context = source_files(target, container=True)
    key = hashlib.sha256(source_zip(context)).hexdigest()
    native = {"id": "/synthetic/runs/native-run", "properties": {
        "runId": "native-run", "status": "Succeeded",
    }}
    commands = Commands(
        [], native, native, ["agent-insights-quality-support"],
        [{"name": "source-" + key, "digest": "sha256:" + "c" * 64}],
    )
    saved = []
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=saved.append, command=commands,
    )
    image = asyncio.run(builder.ensure_image(target, "r", context))
    assert image.endswith("@sha256:" + "c" * 64)
    assert saved[1]["run_id"] == "native-run"
    assert saved[-1]["provider_response"] == native
    assert Path(saved[-1]["context_path"]).exists()


def test_restart_unknown_submission_reconciles_only_the_exact_native_image(target, tmp_path):
    context = source_files(target, container=True)
    saved = []
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=saved.append,
        command=Commands([], CommandResult(0, "")),
    )
    with pytest.raises(QualityError, match="acr_build_identity_missing"):
        asyncio.run(builder.ensure_image(target, "r", context))
    known = saved[-1]
    commands = Commands(["agent-insights-quality-support"], [
        {"name": "source-unrelated", "digest": "sha256:" + "b" * 64},
        {"name": known["tag"], "digest": "sha256:" + "a" * 64},
    ])
    restarted = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=saved.append,
        records={known["artifact_key"]: known}, command=commands,
    )
    image = asyncio.run(restarted.ensure_image(target, "r", context))
    assert image.endswith("@sha256:" + "a" * 64)
    assert saved[-1]["context_path"] == known["context_path"]
    assert saved[-1]["state"] == "completed"
    assert all(call[1] == "repository" for call in commands.calls)


def test_pending_native_run_poll_rejects_another_run_identity(target, tmp_path):
    context = source_files(target, container=True)
    key = hashlib.sha256(source_zip(context)).hexdigest()
    record = {"artifact_key": key, "state": "pending", "run_id": "expected-native-run"}
    commands = Commands([], {"properties": {"runId": "unrelated-run", "status": "Succeeded"}})
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=lambda value: None,
        records={key: record}, command=commands,
    )
    with pytest.raises(QualityError, match="acr_run_identity_mismatch"):
        asyncio.run(builder.ensure_image(target, "r", context))
    assert len(commands.calls) == 2


def test_failed_cli_result_remains_unknown_with_its_context(target, tmp_path):
    saved = []
    builder = AcrImageBuilder(
        "exampleregistry", workspace=tmp_path, persist=saved.append,
        command=Commands([], CommandResult(1, "Synthetic CLI failure")),
    )
    with pytest.raises(QualityError, match="acr_build_unknown") as error:
        asyncio.run(builder.ensure_image(target, "r", source_files(target, container=True)))
    assert error.value.request_accepted is None
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert saved[-1]["state"] == "unknown"
    assert Path(saved[-1]["context_path"]).is_dir()


def test_cancelled_cli_command_drains_the_worker_before_returning(tmp_path, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    command = AzureCommandRunner()

    def blocking(arguments, cwd):
        entered.set()
        assert release.wait(timeout=5), "Worker was not released"
        assert cwd.is_dir()
        finished.set()
        return CommandResult(0, "{}")

    monkeypatch.setattr(command, "_run", blocking)

    async def exercise():
        task = asyncio.create_task(command.run(["synthetic-command"], cwd=tmp_path))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("stdout,state", [
    ('{"properties":{"runId":"native-run","status":"Queued"}}', "pending"),
    ("", "unknown"),
])
def test_cancelled_build_drains_and_checkpoints_its_actual_outcome(
    target, tmp_path, stdout, state,
):
    saved = []

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingCommands:
            async def run(self, arguments, *, cwd=None):
                if arguments[1] == "repository":
                    return CommandResult(0, "[]")
                entered.set()
                await release.wait()
                assert (cwd / "v0" / "source" / "app.py").is_file()
                return CommandResult(0, stdout)

        builder = AcrImageBuilder(
            "exampleregistry", workspace=tmp_path, persist=saved.append, command=BlockingCommands(),
        )
        task = asyncio.create_task(builder.ensure_image(target, "r", source_files(target, container=True)))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert saved[-1]["state"] == state
        assert Path(saved[-1]["context_path"]).is_dir()
        if state == "pending":
            assert saved[-1]["run_id"] == "native-run"

    asyncio.run(exercise())
