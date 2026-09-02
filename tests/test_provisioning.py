from __future__ import annotations

import json
import io
import shutil
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.provisioning import (
    FoundryProvisioner,
    RemoteHttpError,
    _build_support_images,
    _materialize_support_build_context,
    _monitor_inventory_matches,
    _support_build_context_digest,
    _support_build_context_digest_at_commit,
    _support_build_context_manifest,
    _support_wheelhouse,
    _version_from_response,
    deterministic_zip,
)
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.util import ROOT


def test_hosted_package_is_deterministic_and_issue_specific(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('synthetic')\n", encoding="utf-8")
    first_issue = tmp_path / "first.yaml"
    first_issue.write_text("issue_id: issue-001\n", encoding="utf-8")
    second_issue = tmp_path / "second.yaml"
    second_issue.write_text("issue_id: issue-002\n", encoding="utf-8")
    first = deterministic_zip(source, extra=first_issue)
    assert first == deterministic_zip(source, extra=first_issue)
    assert first != deterministic_zip(source, extra=second_issue)


def test_agent_create_response_requires_direct_server_assigned_version() -> None:
    assert _version_from_response(
        {
            "id": "healthcare-agent",
            "version": "1",
        }
    ) == "1"


def test_initial_agent_create_accepts_nested_returned_version() -> None:
    assert _version_from_response(
        {
            "id": "healthcare-agent",
            "versions": {"latest": {"version": "1"}},
        }
    ) == "1"


def test_fresh_version_creation_does_not_reuse_existing_digest() -> None:
    client = FoundryProvisioner(
        RuntimeProfile(
            name="staging",
            project_name="aiq-staging-swedencentral",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )
    client._agent_exists = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
    client._recover_exact_version = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fresh creation must not rediscover an old version")
        )
    )
    requested = []

    def request(method, path, **_kwargs):
        requested.append((method, path))
        return {"version": "2"}

    details = {
        "version": "2",
        "metadata": {
            "aiq_profile": "staging",
            "aiq_logical_version": "issue-001",
            "aiq_content_digest": "sha256:" + ("a" * 64),
        },
    }
    client._request = request  # type: ignore[method-assign]
    client._wait_active = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: details
    )
    version, observed = client.create_version_for_readiness(
        agent={"name": "weather-agent-issue-001", "type": "prompt"},
        logical_version="issue-001",
        artifact={
            "kind": "prompt",
            "definition": {},
            "content_digest": "sha256:" + ("a" * 64),
        },
    )
    assert version == "2"
    assert observed == details
    assert requested == [
        ("POST", "/agents/weather-agent-issue-001/versions")
    ]


def test_monitor_inventory_rejects_duplicates_and_unexpected_agents() -> None:
    expected = {"weather-agent": "monitor-weather"}
    assert _monitor_inventory_matches(
        [{"agent_name": "weather-agent", "id": "monitor-weather"}],
        expected,
    )
    assert not _monitor_inventory_matches(
        [
            {"agent_name": "weather-agent", "id": "monitor-old"},
            {"agent_name": "weather-agent", "id": "monitor-weather"},
        ],
        expected,
    )
    assert not _monitor_inventory_matches(
        [
            {"agent_name": "weather-agent", "id": "monitor-weather"},
            {"agent_name": "unknown-agent", "id": "monitor-unknown"},
        ],
        expected,
    )


def test_monitor_list_get_retries_no_response(monkeypatch) -> None:
    attempts = 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return json.dumps({"data": []}).encode()

    def open_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError("synthetic timeout")
        return Response()

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.urllib.request.urlopen",
        open_request,
    )
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.sleep",
        lambda _: None,
    )
    client = FoundryProvisioner(
        RuntimeProfile(
            name="staging",
            project_name="agent-insights-quality-staging",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )
    assert client._list_monitors() == []
    assert attempts == 2


def test_foundry_error_progress_is_public_safe(monkeypatch) -> None:
    payload = json.dumps(
        {
            "error": {
                "code": "TooManyRequests",
                "message": "private request detail",
            }
        }
    ).encode()

    def open_request(request, **_kwargs):
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {},
            io.BytesIO(payload),
        )

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.urllib.request.urlopen",
        open_request,
    )
    client = FoundryProvisioner(
        RuntimeProfile(
            name="validation",
            project_name="validation",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )
    messages = []
    client.report_progress = messages.append  # type: ignore[method-assign]
    with pytest.raises(RemoteHttpError):
        client._request(
            "POST",
            "/agents",
            body=b"{}",
            hosted=False,
        )
    assert messages == [
        "Foundry POST rejected: status=429; code=TooManyRequests"
    ]


def test_openai_response_cleanup_route_omits_foundry_api_version(
    monkeypatch,
) -> None:
    urls = []

    class Response:
        status = 404

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b""

    def open_request(request, **_kwargs):
        urls.append(request.full_url)
        return Response()

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.urllib.request.urlopen",
        open_request,
    )
    client = FoundryProvisioner(
        RuntimeProfile(
            name="validation",
            project_name="validation",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )

    assert client.response_exists("synthetic-response") is False
    assert urls == [
        "https://example.invalid/openai/v1/responses/synthetic-response"
    ]


def test_early_agent_create_not_found_retries_with_capped_backoff(
    monkeypatch,
) -> None:
    now = [10.0]
    sleeps = []
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.monotonic",
        lambda: now[0],
    )

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.sleep",
        sleep,
    )
    client = FoundryProvisioner(
        RuntimeProfile(
            name="validation",
            project_name="validation",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )
    client._project_ready_at = 0.0
    client._find_version = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    client._agent_exists = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    client._recover_exact_version = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: "1"
    )
    client._wait_active = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    calls = 0

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RemoteHttpError(
                404,
                "NotFound",
                "Project propagation pending",
                "POST /agents",
            )
        return {"version": "1"}

    client._request = request  # type: ignore[method-assign]
    progress = []
    client.report_progress = progress.append  # type: ignore[method-assign]
    version = client.ensure_version(
        agent={"name": "synthetic-agent", "type": "prompt"},
        logical_version="v0",
        artifact={
            "kind": "prompt",
            "definition": {},
            "content_digest": "sha256:" + ("a" * 64),
        },
    )
    assert version == "1"
    assert calls == 2
    assert sleeps == [5]
    assert progress == [
        "validation/synthetic-agent/v0: Agent-create propagation pending "
        "at 10s; retrying in 5s"
    ]


def test_agent_create_not_found_stops_at_propagation_deadline(
    monkeypatch,
) -> None:
    now = [0.0]
    sleeps = []
    monkeypatch.setattr(
        "agent_insights_quality.provisioning._AGENT_CREATE_PROPAGATION_SECONDS",
        6,
    )
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.monotonic",
        lambda: now[0],
    )

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.sleep",
        sleep,
    )
    client = FoundryProvisioner(
        RuntimeProfile(
            name="validation",
            project_name="validation",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )
    client._project_ready_at = 0.0
    client._find_version = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    client._agent_exists = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    client._request = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RemoteHttpError(
                404,
                "NotFound",
                "Project propagation pending",
                "POST /agents",
            )
        )
    )
    with pytest.raises(RemoteHttpError):
        client.ensure_version(
            agent={"name": "synthetic-agent", "type": "prompt"},
            logical_version="v0",
            artifact={
                "kind": "prompt",
                "definition": {},
                "content_digest": "sha256:" + ("a" * 64),
            },
        )
    assert sleeps == [5, 1]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/agents/synthetic"),
        ("DELETE", "/agents/synthetic"),
        ("POST", "/agents/synthetic/versions"),
    ],
)
def test_non_create_not_found_is_not_retried(
    monkeypatch,
    method,
    path,
) -> None:
    calls = 0
    payload = json.dumps(
        {"error": {"code": "NotFound", "message": "Synthetic missing"}}
    ).encode()

    def open_request(request, **_kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            {},
            io.BytesIO(payload),
        )

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.urllib.request.urlopen",
        open_request,
    )
    client = FoundryProvisioner(
        RuntimeProfile(
            name="validation",
            project_name="validation",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )
    client._project_ready_at = 0.0
    with pytest.raises(RemoteHttpError):
        client._request(method, path, hosted=False)
    assert calls == 1


def test_exact_new_version_readiness_not_found_retries_then_succeeds(
    monkeypatch,
) -> None:
    now = [10.0]
    sleeps = []
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.monotonic",
        lambda: now[0],
    )

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.sleep",
        sleep,
    )
    client = FoundryProvisioner(
        RuntimeProfile(
            name="validation",
            project_name="validation",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )
    client._project_ready_at = 0.0
    calls = 0

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RemoteHttpError(
                404,
                "NotFound",
                "Exact version propagation pending",
                "GET /agents/synthetic-agent/versions/1",
            )
        return {
            "status": "active",
            "metadata": {
                "aiq_profile": "validation",
                "aiq_logical_version": "v0",
                "aiq_content_digest": "sha256:" + ("a" * 64),
            },
        }

    client._request = request  # type: ignore[method-assign]
    progress = []
    client.report_progress = progress.append  # type: ignore[method-assign]
    details = client._wait_active(
        "synthetic-agent",
        "1",
        hosted=False,
        expected_metadata={
            "aiq_profile": "validation",
            "aiq_logical_version": "v0",
            "aiq_content_digest": "sha256:" + ("a" * 64),
        },
        not_found_confirmed_at=0.0,
    )
    assert details["status"] == "active"
    assert calls == 2
    assert sleeps == [5]
    assert progress == [
        "validation/synthetic-agent/v0: exact version readiness "
        "propagation pending at 10s; retrying in 5s"
    ]


def test_exact_new_version_readiness_not_found_stops_at_deadline(
    monkeypatch,
) -> None:
    now = [0.0]
    sleeps = []
    monkeypatch.setattr(
        "agent_insights_quality.provisioning._AGENT_CREATE_PROPAGATION_SECONDS",
        6,
    )
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.monotonic",
        lambda: now[0],
    )

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.sleep",
        sleep,
    )
    client = FoundryProvisioner(
        RuntimeProfile(
            name="validation",
            project_name="validation",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )
    client._project_ready_at = 0.0
    client._request = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RemoteHttpError(
                404,
                "NotFound",
                "Exact version propagation pending",
                "GET /agents/synthetic-agent/versions/1",
            )
        )
    )
    with pytest.raises(RemoteHttpError):
        client._wait_active(
            "synthetic-agent",
            "1",
            hosted=False,
            expected_metadata={
                "aiq_profile": "validation",
                "aiq_logical_version": "v0",
                "aiq_content_digest": "sha256:" + ("a" * 64),
            },
            not_found_confirmed_at=0.0,
        )
    assert sleeps == [5, 1]


@pytest.mark.parametrize(
    ("confirmed", "status", "code"),
    [
        (False, 404, "NotFound"),
        (True, 403, "Forbidden"),
        (True, 404, "DifferentCode"),
    ],
)
def test_version_readiness_retry_requires_exact_listing_and_not_found(
    monkeypatch,
    confirmed,
    status,
    code,
) -> None:
    sleeps = []
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.monotonic",
        lambda: 10.0,
    )
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.sleep",
        sleeps.append,
    )
    client = FoundryProvisioner(
        RuntimeProfile(
            name="validation",
            project_name="validation",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )
    client._project_ready_at = 0.0
    client._request = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RemoteHttpError(
                status,
                code,
                "Synthetic readiness failure",
                "GET /agents/synthetic-agent/versions/1",
            )
        )
    )
    with pytest.raises(RemoteHttpError):
        client._wait_active(
            "synthetic-agent",
            "1",
            hosted=False,
            expected_metadata={
                "aiq_profile": "validation",
                "aiq_logical_version": "v0",
                "aiq_content_digest": "sha256:" + ("a" * 64),
            },
            not_found_confirmed_at=0.0 if confirmed else None,
        )
    assert sleeps == []


def test_support_wheelhouse_reuses_exact_requirements_cache(
    monkeypatch,
    tmp_path,
) -> None:
    root = tmp_path / "support-ticket-agent"
    requirements = root / "v0" / "requirements.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("synthetic-package==1.0\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.runtime_root",
        lambda: tmp_path / "runtime",
    )
    calls = 0

    def run(arguments, **_kwargs):
        nonlocal calls
        calls += 1
        destination = Path(arguments[arguments.index("--dest") + 1])
        (destination / "synthetic.whl").write_bytes(b"synthetic-wheel")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.subprocess.run",
        run,
    )
    reporter = ProgressReporter("test")
    first = _support_wheelhouse(root, reporter)
    second = _support_wheelhouse(root, reporter)
    assert first == second
    assert calls == 1
    assert (first / "receipt.json").is_file()


def test_support_images_skip_build_when_source_digest_tags_exist(
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.subprocess.run",
        lambda arguments, **_kwargs: (
            calls.append(arguments)
            or type("Result", (), {"returncode": 0})()
        ),
    )
    monkeypatch.setattr(
        "agent_insights_quality.provisioning._existing_acr_image",
        lambda registry, tag, **_kwargs: (
            f"{registry}.azurecr.io/agent-insights-quality-support@"
            f"sha256:{tag:0>64}"
        ),
    )
    monkeypatch.setattr(
        "agent_insights_quality.provisioning._support_wheelhouse",
        lambda *_args, **_kwargs: pytest.fail(
            "wheelhouse should not be prepared for exact cache hits"
        ),
    )
    agents, _ = load_catalogs()
    support = next(
        item
        for item in agents["agents"]
        if item["name"] == "support-ticket-agent"
    )
    images = _build_support_images(
        RuntimeProfile(
            name="validation",
            project_name="validation",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/insights",
            registry_path=ROOT / "registry.json",
            container_registry_name="syntheticregistry",
        ),
        support,
    )
    assert len(images) == 9
    assert all(arguments[1:3] == ["acr", "login"] for arguments in calls)


@pytest.mark.parametrize(
    ("relative_path", "changed_versions"),
    [
        ("v0/source/app.py", {"v0"}),
        ("v0/implementation.yaml", {"v0"}),
        ("issues/issue-029/source/app.py", {"issue-029"}),
        ("issues/issue-029/implementation.yaml", {"issue-029"}),
        (
            "v0/Dockerfile",
            {"v0", *{f"issue-{index:03d}" for index in range(29, 37)}},
        ),
        (
            "v0/requirements.txt",
            {"v0", *{f"issue-{index:03d}" for index in range(29, 37)}},
        ),
        (
            "v0/container.yaml",
            {"v0", *{f"issue-{index:03d}" for index in range(29, 37)}},
        ),
        (
            "v0/package.py",
            {"v0", *{f"issue-{index:03d}" for index in range(29, 37)}},
        ),
    ],
)
def test_support_build_context_dependencies_are_isolated(
    tmp_path: Path,
    relative_path: str,
    changed_versions: set[str],
) -> None:
    root = tmp_path / "support-ticket-agent"
    shutil.copytree(ROOT / "agents" / "support-ticket-agent", root)
    versions = ["v0", *[f"issue-{index:03d}" for index in range(29, 37)]]
    before = {
        version: _support_build_context_digest(root, version)
        for version in versions
    }
    changed = root / relative_path
    changed.write_bytes(changed.read_bytes() + b"\n# synthetic digest change\n")
    after = {
        version: _support_build_context_digest(root, version)
        for version in versions
    }
    assert {
        version for version in versions if before[version] != after[version]
    } == changed_versions


def test_support_issue_build_context_uses_complete_source_authority(
    tmp_path: Path,
) -> None:
    root = ROOT / "agents" / "support-ticket-agent"
    issue_root = root / "issues" / "issue-029"
    manifest = _support_build_context_manifest(root, "issue-029")
    context = tmp_path / "context"
    _materialize_support_build_context(manifest, context)

    expected_sources = {
        path.relative_to(issue_root / "source").as_posix(): path.read_bytes()
        for path in sorted((issue_root / "source").rglob("*"))
        if path.is_file()
    }
    actual_sources = {
        path.relative_to(context / "v0" / "source").as_posix(): path.read_bytes()
        for path in sorted((context / "v0" / "source").rglob("*"))
        if path.is_file()
    }
    assert actual_sources == expected_sources
    assert (context / "v0" / "implementation.yaml").read_bytes() == (
        issue_root / "implementation.yaml"
    ).read_bytes()
    assert {
        path.relative_to(context).as_posix()
        for path in context.rglob("*")
        if path.is_file()
    } == set(manifest)


def test_support_context_commit_requires_exact_git_tree_membership(
    monkeypatch,
) -> None:
    root = ROOT / "agents" / "support-ticket-agent"
    manifest = _support_build_context_manifest(root, "issue-029")
    retained = b"\0".join(
        str(path.relative_to(ROOT)).replace("\\", "/").encode("utf-8")
        for path in manifest.values()
    ) + b"\0"
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1] == "ls-tree":
            return SimpleNamespace(returncode=0, stdout=retained)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.subprocess.run",
        run,
    )
    digest = _support_build_context_digest(
        root,
        "issue-029",
    )
    assert _support_build_context_digest_at_commit(
        root,
        "issue-029",
        "a" * 40,
    ) == digest
    assert [arguments[1] for arguments in calls] == ["ls-tree", "diff"]

    retained = retained.split(b"\0", 1)[1]
    assert _support_build_context_digest_at_commit(
        root,
        "issue-029",
        "a" * 40,
    ) is None
