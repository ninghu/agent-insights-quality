from __future__ import annotations

import json
import io
import urllib.error
from pathlib import Path

import pytest

from agent_insights_quality.provisioning import (
    FoundryProvisioner,
    RemoteHttpError,
    _monitor_inventory_matches,
    _version_from_response,
    deterministic_zip,
)
from agent_insights_quality.profiles import RuntimeProfile


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


def test_agent_create_response_uses_nested_version_not_agent_id() -> None:
    assert _version_from_response(
        {
            "id": "healthcare-agent",
            "versions": {"latest": {"version": "1"}},
        }
    ) == "1"


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
        return {"versions": {"latest": {"version": "1"}}}

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
