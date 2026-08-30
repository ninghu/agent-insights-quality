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
