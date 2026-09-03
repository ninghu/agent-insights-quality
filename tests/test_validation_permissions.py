from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_credentials import LocalAzureOperator
from agent_insights_quality.validation_permissions import (
    assert_validation_permissions,
)


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return f"header.{payload.rstrip('=')}.signature"


def _profile() -> RuntimeProfile:
    prefix = "/subscriptions/synthetic/resourceGroups/synthetic"
    return RuntimeProfile(
        name="staging",
        project_name="staging",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id=(
            f"{prefix}/providers/Microsoft.Insights/components/g30"
        ),
        registry_path=Path("registry.json"),
        account_name="synthetic-account",
        container_registry_name="synthetic-registry",
        registry_storage_account_name="synthetic-storage",
        account_resource_id=(
            f"{prefix}/providers/Microsoft.CognitiveServices/accounts/account"
        ),
        telemetry_resource_set="g30",
    )


def _operator() -> LocalAzureOperator:
    credential = SimpleNamespace(
        get_token=lambda _scope: SimpleNamespace(
            token=_jwt(
                {
                    "tid": "synthetic-tenant",
                    "oid": "synthetic-user",
                    "scp": "Application.ReadWrite.All",
                }
            )
        )
    )
    return LocalAzureOperator(
        credential=credential,
        object_id="synthetic-user",
        tenant_id="synthetic-tenant",
        subscription_id="synthetic",
        operator_reference="sha256:" + ("a" * 64),
    )


def test_destructive_permission_preflight_covers_every_scope(monkeypatch) -> None:
    scopes = []

    def permissions(scope):
        scopes.append(scope)
        return [{"actions": ["*"], "notActions": [], "dataActions": ["*"], "notDataActions": []}]

    monkeypatch.setattr(
        "agent_insights_quality.validation_permissions._effective_permissions",
        permissions,
    )
    assert_validation_permissions(_profile(), _operator())
    assert any("Microsoft.CognitiveServices/accounts/account" in item for item in scopes)
    assert any("Microsoft.Insights/components/g30" in item for item in scopes)
    assert any("Microsoft.ContainerRegistry/registries" in item for item in scopes)


def test_destructive_permission_preflight_rejects_missing_delete(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_permissions._effective_permissions",
        lambda _scope: [
            {
                "actions": ["*/read", "*/write"],
                "notActions": [],
                "dataActions": ["*/read", "*/write"],
                "notDataActions": [],
            }
        ],
    )
    with pytest.raises(ContractError, match="delete permissions"):
        assert_validation_permissions(_profile(), _operator())


def test_registry_preflight_matches_reviewed_legacy_roles(monkeypatch) -> None:
    def permissions(scope):
        if "Microsoft.ContainerRegistry/registries" in scope:
            return [
                {
                    "actions": [
                        "Microsoft.ContainerRegistry/registries/pull/read",
                        "Microsoft.ContainerRegistry/registries/push/write",
                    ],
                    "notActions": [],
                    "dataActions": [],
                    "notDataActions": [],
                },
                {
                    "actions": [
                        "Microsoft.ContainerRegistry/registries/artifacts/delete"
                    ],
                    "notActions": [],
                    "dataActions": [],
                    "notDataActions": [],
                },
            ]
        return [
            {
                "actions": ["*"],
                "notActions": [],
                "dataActions": ["*"],
                "notDataActions": [],
            }
        ]

    monkeypatch.setattr(
        "agent_insights_quality.validation_permissions._effective_permissions",
        permissions,
    )
    assert_validation_permissions(_profile(), _operator())


def test_registry_preflight_rejects_abac_roles_for_legacy_registry(
    monkeypatch,
) -> None:
    def permissions(scope):
        if "Microsoft.ContainerRegistry/registries" in scope:
            return [
                {
                    "actions": [],
                    "notActions": [],
                    "dataActions": [
                        "Microsoft.ContainerRegistry/registries/repositories/"
                        "content/*"
                    ],
                    "notDataActions": [],
                }
            ]
        return [
            {
                "actions": ["*"],
                "notActions": [],
                "dataActions": ["*"],
                "notDataActions": [],
            }
        ]

    monkeypatch.setattr(
        "agent_insights_quality.validation_permissions._effective_permissions",
        permissions,
    )
    with pytest.raises(ContractError, match="ContainerRegistry"):
        assert_validation_permissions(_profile(), _operator())
