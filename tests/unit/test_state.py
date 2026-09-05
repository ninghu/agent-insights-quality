from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from agent_insights_quality import state
from agent_insights_quality.state import (
    CheckpointError,
    RuntimeStore,
    StateConflict,
    StateError,
)


@pytest.fixture
def runtime(tmp_path):
    with RuntimeStore("staging", root=tmp_path).ownership() as runtime:
        yield runtime


def test_progress_completion_and_replay(runtime):
    run = runtime.run("synthetic-r1")
    key = "targets/weather/v0/attempts/01/turns/01"
    run.save_progress(key, {"retry_count": 2, "deadline": "2026-09-05T12:00:00Z"})
    assert run.read_completed(key, missing_ok=True) is None
    completed = {"response": {"text": "synthetic"}, "retry_count": 2}
    run.save_completed(key, completed)
    run.save_completed(key, {"retry_count": 2, "response": {"text": "synthetic"}})
    run.save_progress(key, completed)
    assert run.read(key) == completed
    assert run.read_completed(key) == completed
    with pytest.raises(StateConflict):
        run.save_completed(key, {"retry_count": 3})
    with pytest.raises(StateConflict):
        run.save_progress(key, {"retry_count": 3})


def test_source_date_settings_and_target_isolation(runtime):
    first = runtime.run("synthetic-r1")
    second = runtime.run("synthetic-r2")
    metadata = {
        "source": "synthetic-source-one", "run_date": "2026-09-05",
        "settings": {"attempts": 10}, "provider": {"version": "synthetic"},
    }
    first.save_completed("run", metadata)
    second.save_completed("run", {**metadata, "source": "synthetic-source-two"})
    first.save_completed("targets/weather/v0/stages/evidence", {"count": 6})
    first.save_completed("targets/weather/v1/stages/evidence", {"count": 7})
    first.save_completed("targets/travel/v0/stages/evidence", {"count": 8})
    assert first.read("run") == metadata
    assert second.read("run")["source"] == "synthetic-source-two"
    assert first.read("targets/weather/v0/stages/evidence") == {"count": 6}
    assert first.read("targets/weather/v1/stages/evidence") == {"count": 7}
    assert first.read("targets/travel/v0/stages/evidence") == {"count": 8}
    with pytest.raises(StateConflict):
        first.save_completed("run", {**metadata, "source": "synthetic-source-two"})


def test_direct_last_test_and_outboxes_preserve_completed_runs(runtime):
    run = runtime.run("synthetic-r1")
    completed = {"source": "synthetic-source", "run_date": "2026-09-05", "outcome": "FAIL"}
    run.save_completed("targets/weather/v0", completed)
    runtime.staging_index.save_progress("weather/v0", {"run": "synthetic-r1", **completed})
    runtime.staging_index.save_progress("weather/v0", {"run": "synthetic-r2"})
    outbox = runtime.outbox("email")
    outbox.save_completed("synthetic-r1/request", {"subject": "Synthetic"})
    outbox.save_progress("synthetic-r1/send", {"status": "ambiguous"})
    assert run.read("targets/weather/v0") == completed
    assert outbox.read("synthetic-r1/send") == {"status": "ambiguous"}
    assert outbox.read("synthetic-r1/request") == {"subject": "Synthetic"}


def test_saved_artifact_and_completed_result_survive_index_failure(runtime, monkeypatch):
    run = runtime.run("synthetic-r1")
    run.save_artifact("targets/weather/v0/raw", {"records": [{"text": "synthetic"}]})
    run.save_completed("targets/weather/v0/stages/evidence", {"artifact": "targets/weather/v0/raw"})

    def fail_replace(source, destination):
        raise OSError("synthetic private detail")

    monkeypatch.setattr(state, "_replace", fail_replace)
    with pytest.raises(CheckpointError, match="^state_checkpoint_failed$"):
        runtime.staging_index.save_progress("weather/v0", {"run": "synthetic-r1"})
    restarted = RuntimeStore("staging", root=runtime.root).run("synthetic-r1")
    assert restarted.read_completed("targets/weather/v0/stages/evidence") is not None
    assert restarted.read_artifact("targets/weather/v0/raw") == {
        "records": [{"text": "synthetic"}]
    }
    assert not list(runtime.root.rglob(".pending-*"))
    with pytest.raises(StateConflict):
        run.save_artifact("targets/weather/v0/raw", {"records": []})


def test_failed_atomic_replace_preserves_previous_progress(runtime, monkeypatch):
    run = runtime.run("synthetic-r1")
    run.save_progress("stage", {"retry_count": 1})

    def fail_replace(source, destination):
        assert json.loads(source.read_text()) == {"retry_count": 2}
        assert json.loads(destination.read_text()) == {"retry_count": 1}
        raise OSError("synthetic failure")

    monkeypatch.setattr(state, "_replace", fail_replace)
    with pytest.raises(CheckpointError):
        run.save_progress("stage", {"retry_count": 2})
    assert run.read("stage") == {"retry_count": 1}


def test_file_is_synced_before_replacement(runtime, monkeypatch):
    run = runtime.run("synthetic-r1")
    run.save_progress("stage", {"retry_count": 1})
    operations = []
    real_sync = state.os.fsync
    real_replace = state._replace

    def sync(descriptor):
        operations.append("sync")
        real_sync(descriptor)

    def replace(source, destination):
        assert operations[-1] == "sync"
        operations.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(state.os, "fsync", sync)
    monkeypatch.setattr(state, "_replace", replace)
    run.save_completed("stage", {"retry_count": 1})
    assert "replace" in operations


def test_missing_corrupt_and_invalid_records_are_distinct(runtime):
    run = runtime.run("synthetic-r1")
    assert run.read("missing", missing_ok=True) is None
    with pytest.raises(StateError, match="^state_record_missing$"):
        run.read("missing")
    run.save_progress("stage", {"retry_count": 1})
    run.save_completed("stage", {"done": True})
    path = run.directory / "completed" / "stage.json"
    for invalid in ('{', '[]', '{"a":1,"a":2}', '{"a":NaN}'):
        path.write_text(invalid, encoding="utf-8")
        with pytest.raises(StateError, match="^state_record_corrupt$"):
            run.read("stage", missing_ok=True)
    with pytest.raises(StateError, match="^state_record_invalid$"):
        run.save_progress("invalid", {"value": float("inf")})
    with pytest.raises(StateError, match="^state_record_invalid$"):
        run.save_progress("invalid", {1: "not a string key"})
    with pytest.raises(StateError, match="^state_record_invalid$"):
        run.save_progress("invalid", {"nested": {1: "not a string key"}})


def test_corrupt_progress_is_not_silently_replaced(runtime):
    run = runtime.run("synthetic-r1")
    run.save_progress("stage", {"retry_count": 1})
    (run.directory / "progress" / "stage.json").write_text("{", encoding="utf-8")
    with pytest.raises(StateError, match="^state_record_corrupt$"):
        run.save_progress("stage", {"retry_count": 0})


def test_identical_replay_still_requires_durability(runtime, monkeypatch):
    run = runtime.run("synthetic-r1")
    run.save_completed("stage", {"done": True})

    def fail_sync(descriptor):
        raise OSError("synthetic sync failure")

    monkeypatch.setattr(state.os, "fsync", fail_sync)
    with pytest.raises(CheckpointError):
        run.save_completed("stage", {"done": True})
    assert run.read_completed("stage") == {"done": True}


def test_identical_json_does_not_conflate_boolean_and_number(runtime):
    run = runtime.run("synthetic-r1")
    run.save_completed("stage", {"value": True})
    with pytest.raises(StateConflict):
        run.save_completed("stage", {"value": 1})


@pytest.mark.parametrize(
    "key", ["", ".", "..", "../escape", "/root", "a//b", "a/../b", "a\\b",
            "C:\\escape", "a:b", "a.", "a ", "CON", "con", "aux", "lpt1", "A", "a" * 81],
)
def test_reject_path_escape_and_windows_aliases(runtime, key):
    with pytest.raises(StateError, match="^state_path_invalid$"):
        runtime.run("synthetic-r1").save_progress(key, {"x": 1})


def test_reject_unsafe_run_environment_and_outbox(tmp_path):
    with pytest.raises(StateError):
        RuntimeStore("../escape", root=tmp_path)
    runtime = RuntimeStore("daily", root=tmp_path)
    with pytest.raises(StateError):
        runtime.run("a/b")
    with pytest.raises(StateError):
        runtime.outbox("a/b")
    with pytest.raises(StateError):
        runtime.staging_index
    assert not (tmp_path / "runner-v1").exists()


def test_symlink_escape_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "private"
    root.mkdir()
    try:
        (root / "runner-v1").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires OS permission")
    with pytest.raises(StateError, match="^state_path_invalid$"):
        RuntimeStore("daily", root=root)
    assert not list(outside.iterdir())


def test_only_new_environment_namespace_is_written(tmp_path):
    history = tmp_path / "old-history.json"
    history.write_text('{"keep":true}', encoding="utf-8")
    for profile in ("daily", "staging"):
        with RuntimeStore(profile, root=tmp_path).ownership() as runtime:
            runtime.run("synthetic-r1").save_completed("run", {"profile": profile})
    assert history.read_text() == '{"keep":true}'
    assert RuntimeStore("daily", root=tmp_path).run("synthetic-r1").read("run") == {"profile": "daily"}
    assert RuntimeStore("staging", root=tmp_path).run("synthetic-r1").read("run") == {"profile": "staging"}


def test_writes_require_ownership_but_reads_do_not(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    run = runtime.run("synthetic-r1")
    with pytest.raises(StateError, match="^state_not_owned$"):
        run.save_progress("stage", {"retry_count": 1})
    with runtime.ownership():
        run.save_progress("stage", {"retry_count": 1})
        observer = RuntimeStore("daily", root=tmp_path)
        assert observer.run("synthetic-r1").read("stage") == {"retry_count": 1}
        with pytest.raises(StateError, match="^state_owned$"):
            with observer.ownership():
                pytest.fail("Second owner admitted")
        with pytest.raises(StateError, match="^state_owned$"):
            with runtime.ownership():
                pytest.fail("Nested owner admitted")
        run.save_progress("stage", {"retry_count": 2})
    with observer.ownership():
        observer.run("synthetic-r1").save_completed("stage", {"retry_count": 2})


def test_process_ownership_is_nonblocking_and_released(tmp_path):
    program = """
import sys
from pathlib import Path
from agent_insights_quality.state import RuntimeStore, StateError
runtime = RuntimeStore("daily", root=Path(sys.argv[1]))
try:
    with runtime.ownership():
        print("owned")
except StateError as error:
    print(error.code)
"""

    def child():
        return subprocess.run(
            [sys.executable, "-c", program, str(tmp_path)],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()

    with RuntimeStore("daily", root=tmp_path).ownership():
        assert child() == "state_owned"
    assert child() == "owned"


def test_process_crash_releases_os_ownership(tmp_path):
    program = """
import os
import sys
from pathlib import Path
from agent_insights_quality.state import RuntimeStore
with RuntimeStore("daily", root=Path(sys.argv[1])).ownership() as runtime:
    runtime.run("synthetic-r1").save_completed("stage", {"done": True})
    os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)], check=True, timeout=10,
    )
    with RuntimeStore("daily", root=tmp_path).ownership() as runtime:
        assert runtime.run("synthetic-r1").read_completed("stage") == {"done": True}


def test_concurrent_completion_is_serialized(runtime):
    def save(value):
        try:
            runtime.run("synthetic-r1").save_completed("stage", {"winner": value})
            return "saved"
        except StateConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(save, range(4)))
    assert outcomes.count("saved") == 1
    assert outcomes.count("conflict") == 3
    assert isinstance(runtime.run("synthetic-r1").read("stage")["winner"], int)


def test_status_reader_does_not_block_atomic_progress_replacement(runtime, monkeypatch):
    run = runtime.run("synthetic-r1")
    run.save_progress("stage", {"retry_count": 1})
    reader_open = threading.Event()
    release_reader = threading.Event()
    real_load = state.json.load

    def held_read(stream, **kwargs):
        if threading.current_thread().name.startswith("status-reader"):
            reader_open.set()
            assert release_reader.wait(timeout=5)
        return real_load(stream, **kwargs)

    monkeypatch.setattr(state.json, "load", held_read)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="status-reader") as pool:
        future = pool.submit(run.read, "stage")
        try:
            assert reader_open.wait(timeout=5)
            run.save_progress("stage", {"retry_count": 2})
        finally:
            release_reader.set()
        assert future.result(timeout=5) == {"retry_count": 1}
    assert run.read("stage") == {"retry_count": 2}


def test_production_store_cannot_be_redirected_by_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(tmp_path / "wrong"))
    runtime = RuntimeStore("daily")
    assert runtime.root == tmp_path / ".aiq-runtime" / "agent-insights-quality"
    assert runtime.directory == runtime.root / "runner-v1" / "daily"
    assert not runtime.root.exists()
