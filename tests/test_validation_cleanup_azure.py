from __future__ import annotations

import json
import urllib.parse
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_insights_quality.provisioning import RemoteHttpError
from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_cleanup import CleanupPlanItem
from agent_insights_quality.validation_cleanup_azure import (
    AzureValidationCleanupBackend,
)


def _intent(kind: str, discovery_key: str) -> CleanupPlanItem:
    return CleanupPlanItem(
        kind=kind,
        deterministic_name="synthetic-agent/issue-001",
        provider_id="sha256:" + ("a" * 64),
        resolved_provider_id=None,
        intent_reference="sha256:" + ("a" * 64),
        runtime_kind="hosted_code",
        discovery_key=discovery_key,
        parent_id=None,
        authority_id="issue-001",
        state="ambiguous_create",
        cleanup_method="explicit",
        shared_manifest_allowed=False,
    )


def test_partial_agent_version_intent_resolves_without_runtime_topology() -> None:
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._profile = SimpleNamespace(name="validation-cycle")
    backend._client = SimpleNamespace(
        _request=lambda *_args, **_kwargs: {
            "_status": 200,
            "data": [
                {
                    "version": "7",
                    "metadata": {
                        "aiq_profile": "validation-cycle",
                        "aiq_logical_version": "issue-001",
                    },
                }
            ],
        }
    )
    resolved = backend.resolve_intent(
        _intent(
            "provider_agent_version",
            "synthetic-agent|issue-001|provider_agent_version",
        )
    )
    assert resolved is not None
    assert resolved.resolved_provider_id == "synthetic-agent/versions/7"
    assert resolved.deterministic_name == "synthetic-agent/7"


def test_other_cycle_acr_tag_keeps_shared_manifest() -> None:
    digest = "sha256:" + ("b" * 64)
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._profile = SimpleNamespace(
        container_registry_name="synthetic-registry"
    )
    backend._resources = [
        {
            "kind": "acr_manifest",
            "provider_id": digest,
            "resolved_provider_id": None,
            "discovery_key": f"support@{digest}",
            "deterministic_name": "support",
        },
        {
            "kind": "acr_tag",
            "parent_id": digest,
            "deterministic_name": "support:validation-current",
        },
    ]
    backend._run = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {"tags": ["validation-current", "validation-other-cycle"]}
        ),
    )
    assert backend.manifest_is_shared(digest) is True


def test_arm_deployment_absence_uses_deployment_provider() -> None:
    observed = {}
    backend = object.__new__(AzureValidationCleanupBackend)

    def run(arguments, *, expected):
        observed["arguments"] = arguments
        observed["expected"] = expected
        return SimpleNamespace(returncode=3)

    backend._run = run
    item = replace(
        _intent("arm_deployment", "synthetic-deployment"),
        deterministic_name="synthetic-deployment",
        provider_id=(
            "/subscriptions/synthetic/resourceGroups/synthetic/providers/"
            "Microsoft.Resources/deployments/synthetic-deployment"
        ),
        state="delete_intent",
    )
    assert backend.absent(item) is True
    assert observed["arguments"][1:4] == ["deployment", "group", "show"]
    assert observed["expected"] == (0, 3)


def test_resumed_version_cleanup_uses_resolved_provider_version() -> None:
    observed = []
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._client = SimpleNamespace(
        version_exists=lambda agent, version, *, hosted: (
            observed.append((agent, version, hosted)) or False
        )
    )
    item = replace(
        _intent(
            "provider_agent_version",
            "synthetic-agent|issue-001|provider_agent_version",
        ),
        deterministic_name="synthetic-agent/issue-001",
        resolved_provider_id="synthetic-agent/versions/7",
        state="delete_intent",
    )
    assert backend.absent(item) is True
    assert observed == [("synthetic-agent", "7", True)]


def test_version_delete_retries_exact_session_conflict(monkeypatch) -> None:
    calls = []
    sleeps = []
    backend = object.__new__(AzureValidationCleanupBackend)

    def delete_version(agent, version, *, hosted):
        calls.append((agent, version, hosted))
        if len(calls) < 3:
            raise RemoteHttpError(
                409,
                "conflict",
                "Synthetic session deletion is propagating",
                "DELETE private-route",
            )

    backend._client = SimpleNamespace(
        _delete_owned_version=delete_version,
        report_progress=lambda _message: None,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_cleanup_azure.time.sleep",
        sleeps.append,
    )
    item = replace(
        _intent(
            "provider_agent_version",
            "synthetic-agent|issue-001|provider_agent_version",
        ),
        deterministic_name="synthetic-agent/issue-001",
        resolved_provider_id="synthetic-agent/versions/7",
        state="delete_intent",
    )

    backend.delete(item)

    assert calls == [
        ("synthetic-agent", "7", True),
        ("synthetic-agent", "7", True),
        ("synthetic-agent", "7", True),
    ]
    assert sleeps == [5, 10]


@pytest.mark.parametrize(
    ("status", "code"),
    [(409, "OtherConflict"), (403, "conflict")],
)
def test_version_delete_does_not_retry_unrelated_failure(
    monkeypatch,
    status,
    code,
) -> None:
    calls = []
    backend = object.__new__(AzureValidationCleanupBackend)

    def delete_version(*_args, **_kwargs):
        calls.append(None)
        raise RemoteHttpError(
            status,
            code,
            "Synthetic unrelated failure",
            "DELETE private-route",
        )

    backend._client = SimpleNamespace(
        _delete_owned_version=delete_version,
        report_progress=lambda _message: None,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_cleanup_azure.time.sleep",
        pytest.fail,
    )
    item = replace(
        _intent(
            "provider_agent_version",
            "synthetic-agent|issue-001|provider_agent_version",
        ),
        deterministic_name="synthetic-agent/issue-001",
        resolved_provider_id="synthetic-agent/versions/7",
        state="delete_intent",
    )

    with pytest.raises(RemoteHttpError):
        backend.delete(item)
    assert calls == [None]


def test_version_delete_conflict_retry_is_bounded(monkeypatch) -> None:
    calls = []
    sleeps = []
    backend = object.__new__(AzureValidationCleanupBackend)

    def delete_version(*_args, **_kwargs):
        calls.append(None)
        raise RemoteHttpError(
            409,
            "conflict",
            "Synthetic session deletion is propagating",
            "DELETE private-route",
        )

    backend._client = SimpleNamespace(
        _delete_owned_version=delete_version,
        report_progress=lambda _message: None,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_cleanup_azure.time.sleep",
        sleeps.append,
    )
    item = replace(
        _intent(
            "provider_agent_version",
            "synthetic-agent|issue-001|provider_agent_version",
        ),
        deterministic_name="synthetic-agent/issue-001",
        resolved_provider_id="synthetic-agent/versions/7",
        state="delete_intent",
    )

    with pytest.raises(RemoteHttpError):
        backend.delete(item)
    assert len(calls) == 5
    assert sleeps == [5, 10, 20, 30]


def test_discovery_absent_resource_stays_idempotently_absent() -> None:
    backend = object.__new__(AzureValidationCleanupBackend)
    item = replace(
        _intent(
            "provider_agent_version",
            "synthetic-agent|issue-001|provider_agent_version",
        ),
        resolved_provider_id="discovery-absent",
        state="delete_intent",
    )
    assert backend.absent(item) is True


def test_stored_response_intent_uses_exact_agent_scoped_discovery() -> None:
    calls = []
    intent = _intent(
        "stored_response",
        f"synthetic-agent|{'sha256:' + ('a' * 64)}",
    )
    intent = replace(intent, runtime_kind="prompt")
    backend = object.__new__(AzureValidationCleanupBackend)

    def request(method, route, **kwargs):
        calls.append((method, route, kwargs))
        return {
            "_status": 200,
            "data": [
                {
                    "id": "response-synthetic",
                    "metadata": {
                        "validation_intent_reference": intent.intent_reference,
                    },
                }
            ],
        }

    backend._client = SimpleNamespace(_request=request)
    resolved = backend.resolve_intent(intent)
    assert resolved is not None
    assert resolved.resolved_provider_id == "response-synthetic"
    method, route, kwargs = calls[0]
    parsed = urllib.parse.urlsplit(route)
    assert method == "GET"
    assert parsed.path == "/openai/v1/responses"
    assert urllib.parse.parse_qs(parsed.query) == {
        "agent_name": ["synthetic-agent"],
        "limit": ["100"],
    }
    assert kwargs == {"hosted": False, "expected": {200, 404}}


def test_stored_response_discovery_400_fails_closed() -> None:
    intent = _intent(
        "stored_response",
        f"synthetic-agent|{'sha256:' + ('a' * 64)}",
    )
    intent = replace(intent, runtime_kind="prompt")
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._client = SimpleNamespace(
        _request=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RemoteHttpError(
                400,
                "BadRequest",
                "Synthetic rejected discovery",
                "GET private-route",
            )
        )
    )
    with pytest.raises(RemoteHttpError) as raised:
        backend.resolve_intent(intent)
    assert raised.value.status == 400
    assert raised.value.code == "BadRequest"


def test_stored_response_empty_scoped_discovery_is_already_absent() -> None:
    intent = _intent(
        "stored_response",
        f"synthetic-agent|{'sha256:' + ('a' * 64)}",
    )
    intent = replace(intent, runtime_kind="prompt")
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._client = SimpleNamespace(
        _request=lambda *_args, **_kwargs: {"_status": 200, "data": []}
    )
    resolved = backend.resolve_intent(intent)
    assert resolved is not None
    assert resolved.resolved_provider_id == "discovery-absent"
    assert backend.absent(resolved) is True


def test_stored_response_discovery_checks_every_scoped_page() -> None:
    intent = _intent(
        "stored_response",
        f"synthetic-agent|{'sha256:' + ('a' * 64)}",
    )
    intent = replace(intent, runtime_kind="prompt")
    routes = []
    responses = iter(
        [
            {
                "_status": 200,
                "data": [],
                "has_more": True,
                "last_id": "response-page-one",
            },
            {
                "_status": 200,
                "data": [
                    {
                        "id": "response-synthetic",
                        "metadata": {
                            "validation_intent_reference": (
                                intent.intent_reference
                            ),
                        },
                    }
                ],
                "has_more": False,
            },
        ]
    )
    backend = object.__new__(AzureValidationCleanupBackend)

    def request(_method, route, **_kwargs):
        routes.append(route)
        return next(responses)

    backend._client = SimpleNamespace(_request=request)
    resolved = backend.resolve_intent(intent)
    assert resolved is not None
    assert resolved.resolved_provider_id == "response-synthetic"
    assert urllib.parse.parse_qs(
        urllib.parse.urlsplit(routes[1]).query
    ) == {
        "after": ["response-page-one"],
        "agent_name": ["synthetic-agent"],
        "limit": ["100"],
    }


def test_exact_metadata_match_without_identity_fails_closed() -> None:
    intent = _intent(
        "stored_response",
        f"synthetic-agent|{'sha256:' + ('a' * 64)}",
    )
    intent = replace(intent, runtime_kind="prompt")
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._client = SimpleNamespace(
        _request=lambda *_args, **_kwargs: {
            "_status": 200,
            "data": [
                {
                    "metadata": {
                        "validation_intent_reference": intent.intent_reference,
                    },
                }
            ],
        }
    )
    with pytest.raises(ContractError, match="identity is missing"):
        backend.resolve_intent(intent)


def _hosted_response_and_session():
    response = replace(
        _intent(
            "stored_response",
            f"synthetic-agent|{'sha256:' + ('b' * 64)}",
        ),
        provider_id="synthetic-response",
        parent_id="synthetic-session",
        state="created",
    )
    session = {
        "kind": "session",
        "provider_id": "synthetic-session",
        "resolved_provider_id": None,
        "authority_id": response.authority_id,
        "runtime_kind": response.runtime_kind,
        "discovery_key": (
            f"synthetic-agent|{'sha256:' + ('c' * 64)}"
        ),
    }
    return response, session


def test_hosted_ephemeral_response_is_absent_when_session_is_inaccessible() -> None:
    response, session = _hosted_response_and_session()
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._resources = [session]
    backend._client = SimpleNamespace(
        session_exists=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RemoteHttpError(
                403,
                "session_not_accessible",
                "Synthetic inaccessible session",
                "GET private-route",
            )
        )
    )

    assert backend.absent(response) is True


def test_hosted_ephemeral_response_rejects_unrelated_session_403() -> None:
    response, session = _hosted_response_and_session()
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._resources = [session]
    backend._client = SimpleNamespace(
        session_exists=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RemoteHttpError(
                403,
                "Forbidden",
                "Synthetic unrelated rejection",
                "GET private-route",
            )
        )
    )

    with pytest.raises(RemoteHttpError) as raised:
        backend.absent(response)
    assert raised.value.code == "Forbidden"


@pytest.mark.parametrize(
    "change",
    [
        {"parent_id": None},
        {"parent_id": "foreign-session"},
        {"authority_id": "foreign-authority"},
        {"runtime_kind": "hosted_other"},
    ],
)
def test_hosted_ephemeral_response_requires_exact_parent_binding(change) -> None:
    response, session = _hosted_response_and_session()
    response = replace(response, **change)
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._resources = [session]
    backend._client = SimpleNamespace(session_exists=lambda *_args, **_kwargs: False)

    with pytest.raises(ContractError, match="cleanup"):
        backend.absent(response)


def test_hosted_ephemeral_response_rejects_foreign_agent_parent() -> None:
    response, session = _hosted_response_and_session()
    session["discovery_key"] = (
        f"foreign-agent|{'sha256:' + ('c' * 64)}"
    )
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._resources = [session]
    backend._client = SimpleNamespace(session_exists=lambda *_args, **_kwargs: False)

    with pytest.raises(ContractError, match="does not match"):
        backend.absent(response)


def test_hosted_ephemeral_response_rejects_duplicate_parent_binding() -> None:
    response, session = _hosted_response_and_session()
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._resources = [session, dict(session)]
    backend._client = SimpleNamespace(session_exists=lambda *_args, **_kwargs: False)

    with pytest.raises(ContractError, match="ambiguous"):
        backend.absent(response)


def test_hosted_ephemeral_response_deletes_owning_session() -> None:
    response, session = _hosted_response_and_session()
    deleted = []
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._resources = [session]
    backend._client = SimpleNamespace(
        delete_session=lambda agent, session_id: deleted.append(
            (agent, session_id)
        )
    )

    backend.delete(response)

    assert deleted == [("synthetic-agent", "synthetic-session")]


def test_session_intent_resolves_exact_agent_scoped_metadata_match() -> None:
    intent = _intent(
        "session",
        f"synthetic-agent|{'sha256:' + ('a' * 64)}",
    )
    calls = []
    backend = object.__new__(AzureValidationCleanupBackend)

    def request(method, route, **kwargs):
        calls.append((method, route, kwargs))
        return {
            "_status": 200,
            "data": [
                {
                    "id": "session-synthetic",
                    "metadata": {
                        "validation_intent_reference": intent.intent_reference,
                    },
                }
            ],
        }

    backend._client = SimpleNamespace(_request=request)
    resolved = backend.resolve_intent(intent)
    assert resolved is not None
    assert resolved.resolved_provider_id == "session-synthetic"
    assert calls[0][1] == (
        "/agents/synthetic-agent/endpoint/sessions?limit=100"
    )


@pytest.mark.parametrize(
    ("kind", "api_version"),
    [
        ("connection", "2025-06-01"),
        ("project", "2025-06-01"),
        ("role_assignment", "2022-04-01"),
    ],
)
def test_arm_cleanup_uses_explicit_resource_api_version(
    kind,
    api_version,
) -> None:
    calls = []
    backend = object.__new__(AzureValidationCleanupBackend)

    def run(arguments, *, expected):
        calls.append((arguments, expected))
        return SimpleNamespace(returncode=0)

    backend._run = run
    item = replace(
        _intent(kind, f"synthetic-{kind}"),
        deterministic_name=f"synthetic-{kind}",
        provider_id=(
            "/subscriptions/synthetic/resourceGroups/synthetic/providers/"
            f"Synthetic.Provider/{kind}/synthetic"
        ),
        state="created",
    )
    assert backend.absent(item) is False
    backend.delete(item)
    for arguments, expected in calls:
        assert arguments[arguments.index("--api-version") + 1] == api_version
        assert expected == (0, 3)


def test_hosted_intent_is_absent_after_parent_project_404() -> None:
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._find_version_by_logical = lambda *_args, **_kwargs: "1"
    backend._client = SimpleNamespace(
        version_details=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RemoteHttpError(
                404,
                "NotFound",
                "Project not found",
                "GET hosted version",
            )
        )
    )
    resolved = backend.resolve_intent(
        _intent(
            "hosted_deployment",
            "synthetic-agent|issue-001|hosted_deployment",
        )
    )
    assert resolved is not None
    assert resolved.resolved_provider_id == "discovery-absent"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("hosted_identity", "synthetic-instance-client"),
        ("hosted_blueprint", "synthetic-blueprint-reference"),
        ("hosted_deployment", "synthetic-agent-guid"),
    ],
)
def test_hosted_intent_resolves_public_version_topology(
    kind,
    expected,
) -> None:
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._find_version_by_logical = lambda *_args, **_kwargs: "1"
    backend._client = SimpleNamespace(
        version_details=lambda *_args, **_kwargs: {
            "agent_guid": "synthetic-agent-guid",
            "instance_identity": {
                "client_id": "synthetic-instance-client",
            },
            "blueprint_reference": {
                "type": "ManagedAgentIdentityBlueprint",
                "blueprint_id": "synthetic-blueprint-reference",
            },
        }
    )
    discovery_key = f"synthetic-agent|issue-001|{kind}"
    resolved = backend.resolve_intent(_intent(kind, discovery_key))
    assert resolved is not None
    assert resolved.discovery_key == discovery_key
    assert resolved.resolved_provider_id == expected


def test_hosted_topology_is_absent_after_version_cleanup() -> None:
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._find_version_by_logical = lambda *_args, **_kwargs: ""
    assert backend.absent(
        replace(
            _intent(
                "hosted_identity",
                "synthetic-agent|issue-001|hosted_identity",
            ),
            resolved_provider_id="synthetic-instance-client",
        )
    )


def test_hosted_topology_delete_waits_for_parent_cascade(monkeypatch) -> None:
    backend = object.__new__(AzureValidationCleanupBackend)
    checks = iter([False, False, True])
    sleeps = []
    backend.absent = lambda _item: next(checks)
    backend._client = SimpleNamespace(report_progress=pytest.fail)
    monkeypatch.setattr(
        "agent_insights_quality.validation_cleanup_azure.time.sleep",
        sleeps.append,
    )

    backend.delete(
        replace(
            _intent(
                "hosted_blueprint",
                "synthetic-agent|issue-001|hosted_blueprint",
            ),
            resolved_provider_id="synthetic-blueprint-reference",
            state="delete_intent",
        )
    )

    assert sleeps == [5, 5]


@pytest.mark.parametrize(
    ("kind", "details", "error_path"),
    [
        (
            "hosted_identity",
            {"instance_identity": {}},
            "instance_identity.client_id",
        ),
        (
            "hosted_blueprint",
            {
                "blueprint_reference": {
                    "type": "unexpected",
                    "blueprint_id": "synthetic-blueprint-reference",
                }
            },
            "blueprint_reference.type",
        ),
        (
            "hosted_deployment",
            {"agent_guid": []},
            "agent_guid",
        ),
    ],
)
def test_hosted_intent_rejects_malformed_public_version_topology(
    kind,
    details,
    error_path,
) -> None:
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._find_version_by_logical = lambda *_args, **_kwargs: "1"
    backend._client = SimpleNamespace(
        version_details=lambda *_args, **_kwargs: details
    )
    with pytest.raises(ContractError, match=error_path):
        backend.resolve_intent(
            _intent(
                kind,
                f"synthetic-agent|issue-001|{kind}",
            )
        )
