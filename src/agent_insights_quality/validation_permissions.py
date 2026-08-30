from __future__ import annotations

import fnmatch
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_credentials import (
    LocalAzureOperator,
    token_claims,
)


@dataclass(frozen=True)
class PermissionRequirement:
    scope: str
    action: str
    data_action: bool = False


def assert_validation_permissions(
    profile: RuntimeProfile,
    operator: LocalAzureOperator,
) -> None:
    account = profile.account_resource_id.rstrip("/")
    if (
        not account
        or not profile.application_insights_resource_id
        or not profile.container_registry_name
    ):
        raise ContractError("Validation permission substrate is incomplete")
    resource_group = account.split("/providers/", 1)[0]
    registry = (
        f"{resource_group}/providers/Microsoft.ContainerRegistry/registries/"
        f"{profile.container_registry_name}"
    )
    requirements = [
        *(
            PermissionRequirement(resource_group, action)
            for action in (
                "Microsoft.Resources/deployments/read",
                "Microsoft.Resources/deployments/write",
                "Microsoft.Resources/deployments/delete",
                "Microsoft.Resources/deployments/cancel/action",
                "Microsoft.Authorization/roleAssignments/read",
                "Microsoft.Authorization/roleAssignments/write",
                "Microsoft.Authorization/roleAssignments/delete",
            )
        ),
        *(
            PermissionRequirement(account, action)
            for action in (
                "Microsoft.CognitiveServices/accounts/projects/read",
                "Microsoft.CognitiveServices/accounts/projects/write",
                "Microsoft.CognitiveServices/accounts/projects/delete",
                "Microsoft.CognitiveServices/accounts/projects/connections/read",
                "Microsoft.CognitiveServices/accounts/projects/connections/write",
                "Microsoft.CognitiveServices/accounts/projects/connections/delete",
            )
        ),
        PermissionRequirement(
            profile.application_insights_resource_id,
            "Microsoft.Insights/components/read",
        ),
        PermissionRequirement(
            profile.application_insights_resource_id,
            "Microsoft.Insights/components/query/read",
        ),
        PermissionRequirement(
            registry,
            "Microsoft.ContainerRegistry/registries/repositories/content/read",
            data_action=True,
        ),
        PermissionRequirement(
            registry,
            "Microsoft.ContainerRegistry/registries/repositories/content/write",
            data_action=True,
        ),
        PermissionRequirement(
            registry,
            "Microsoft.ContainerRegistry/registries/repositories/content/delete",
            data_action=True,
        ),
        *(
            PermissionRequirement(account, action, data_action=True)
            for action in (
                "Microsoft.CognitiveServices/accounts/AIServices/agents/read",
                "Microsoft.CognitiveServices/accounts/AIServices/agents/write",
                "Microsoft.CognitiveServices/accounts/AIServices/agents/delete",
                "Microsoft.CognitiveServices/accounts/AIServices/agents/action",
                "Microsoft.CognitiveServices/accounts/AIServices/agents/sessions/read",
                "Microsoft.CognitiveServices/accounts/AIServices/agents/sessions/write",
                "Microsoft.CognitiveServices/accounts/AIServices/agents/sessions/delete",
                "Microsoft.CognitiveServices/accounts/AIServices/agents/responses/read",
                "Microsoft.CognitiveServices/accounts/AIServices/agents/responses/write",
                "Microsoft.CognitiveServices/accounts/AIServices/agents/responses/delete",
            )
        ),
    ]
    by_scope: dict[str, list[PermissionRequirement]] = {}
    for requirement in requirements:
        by_scope.setdefault(requirement.scope, []).append(requirement)
    missing: list[str] = []
    for scope, scoped in by_scope.items():
        permissions = _effective_permissions(scope)
        missing.extend(
            requirement.action
            for requirement in scoped
            if not _allows(permissions, requirement)
        )
    graph = operator.credential.get_token(
        "https://graph.microsoft.com/.default"
    )
    scopes = set(str(token_claims(str(graph.token)).get("scp") or "").split())
    if not (
        {"Application.ReadWrite.All", "Directory.AccessAsUser.All"} & scopes
    ):
        missing.append("Microsoft.Graph/servicePrincipals/delete")
    if missing:
        raise ContractError(
            "Local Azure operator lacks required validation create/delete permissions: "
            + ", ".join(sorted(missing))
        )


def _effective_permissions(scope: str) -> list[Mapping[str, Any]]:
    process = subprocess.run(
        [
            azure_cli(),
            "rest",
            "--method",
            "get",
            "--url",
            "https://management.azure.com"
            + scope
            + "/providers/Microsoft.Authorization/permissions"
            + "?api-version=2022-04-01",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        raise ContractError("Validation permissions could not be queried")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ContractError("Validation permissions response is invalid") from error
    permissions = value.get("value") if isinstance(value, Mapping) else None
    if not isinstance(permissions, list) or not all(
        isinstance(item, Mapping) for item in permissions
    ):
        raise ContractError("Validation permissions response is invalid")
    return permissions


def _allows(
    permissions: Sequence[Mapping[str, Any]],
    requirement: PermissionRequirement,
) -> bool:
    allow_field = "dataActions" if requirement.data_action else "actions"
    deny_field = "notDataActions" if requirement.data_action else "notActions"
    return any(
        _matches_any(permission.get(allow_field), requirement.action)
        and not _matches_any(permission.get(deny_field), requirement.action)
        for permission in permissions
    )


def _matches_any(patterns: Any, action: str) -> bool:
    return isinstance(patterns, list) and any(
        isinstance(pattern, str)
        and fnmatch.fnmatchcase(action.casefold(), pattern.casefold())
        for pattern in patterns
    )
