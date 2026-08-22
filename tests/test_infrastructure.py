from __future__ import annotations

import re
import tomllib

from agent_insights_quality.contracts import ROOT


def _bicep(path: str) -> str:
    return (ROOT / "infra" / path).read_text(encoding="utf-8")


def _resource(source: str, name: str) -> str:
    match = re.search(
        rf"resource {name}\b(?P<body>.*?)(?=\nresource |\Z)",
        source,
        re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def test_storage_account_name_stays_within_azure_limit() -> None:
    main = _bicep("main.bicep")
    persistent = _bicep("modules/persistent.bicep")
    assert re.search(r"@maxLength\(12\)\s+param uniqueSuffix string", main)
    assert re.search(r"@maxLength\(12\)\s+param uniqueSuffix string", persistent)
    prefix = re.search(r"var storageName = '([^']+)\$\{uniqueSuffix\}'", persistent)
    assert prefix is not None
    assert len(prefix.group(1)) + 12 <= 24


def test_terra_deployments_are_serialized() -> None:
    persistent = _bicep("modules/persistent.bicep")
    terra_insights = re.search(
        r"resource terraInsights\b(?P<body>.*?)(?=\nresource |\Z)",
        persistent,
        re.DOTALL,
    )
    assert terra_insights is not None
    assert re.search(
        r"dependsOn:\s*\[\s*terraAgents\s*\]",
        terra_insights.group("body"),
    )


def test_app_insights_connection_uses_arm_resolved_api_key() -> None:
    qualification = _bicep("modules/qualification-project.bicep")
    connection = re.search(
        r"resource appInsightsConnection\b(?P<body>.*?)(?=\nresource |\Z)",
        qualification,
        re.DOTALL,
    )
    assert connection is not None
    body = connection.group("body")
    assert "authType: 'ApiKey'" in body
    assert re.search(
        r"credentials:\s*\{\s*key:\s*"
        r"applicationInsights\.properties\.ConnectionString\s*\}",
        body,
    )
    assert "ApiType: 'Azure'" in body
    assert "ResourceId: applicationInsights.id" in body
    assert "purpose: 'agent-insights-quality'" in body
    assert "owner_reference: automationOwner" in body
    assert "expires_on: expiresOn" in body


def test_project_connections_support_deterministic_suffixes_without_breaking_base() -> None:
    qualification = _bicep("modules/qualification-project.bicep")
    assert re.search(
        r"@maxLength\(32\)\s+param connectionNameSuffix string = ''",
        qualification,
    )
    assert (
        "var normalizedConnectionSuffix = empty(connectionNameSuffix) ? '' : "
        "'-${connectionNameSuffix}'"
        in qualification
    )
    assert (
        "var appInsightsConnectionName = "
        "'application-insights${normalizedConnectionSuffix}'"
        in qualification
    )
    assert (
        "var containerRegistryConnectionName = "
        "'container-registry${normalizedConnectionSuffix}'"
        in qualification
    )
    assert "name: appInsightsConnectionName" in qualification
    assert "name: containerRegistryConnectionName" in qualification


def test_connection_string_has_no_parameter_output_or_cli_surface() -> None:
    infra_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "infra").rglob("*.bicep"))
    )
    declarations = re.findall(
        r"^\s*(?:param|output)\s+([A-Za-z_][A-Za-z0-9_]*)",
        infra_sources,
        re.MULTILINE,
    )
    assert not any(
        "connectionstring" in re.sub(r"[^a-z0-9]", "", name.lower())
        for name in declarations
    )
    assert infra_sources.count("ConnectionString") == 1
    assert (
        "key: applicationInsights.properties.ConnectionString"
        in infra_sources
    )

    cli = (ROOT / "src" / "agent_insights_quality" / "cli.py").read_text(
        encoding="utf-8"
    )
    assert "connection-string" not in cli.lower()
    assert "connection_string" not in cli.lower()
    assert "ConnectionString" not in cli


def test_project_roles_tags_and_retention_contracts_are_preserved() -> None:
    qualification = _bicep("modules/qualification-project.bicep")
    persistent = _bicep("modules/persistent.bicep")

    assert "43d0d8ad-25c7-4714-9337-8ba259a9fe05" in qualification
    assert "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd" in qualification
    assert "scope: applicationInsights" in qualification
    assert "scope: account" in qualification
    assert qualification.count("principalId: project.identity.principalId") == 3

    for tag in (
        "purpose: 'agent-insights-quality'",
        "agentInsightsQualityQualification: 'true'",
        "automationOwner: automationOwner",
    ):
        assert tag in persistent
        assert tag in qualification
    assert "reportDate: reportDate" in qualification
    assert "expiresOn: expiresOn" in qualification
    assert "catalogVersion: catalogVersion" in qualification
    assert "retentionInDays: 90" in persistent
    assert "daysAfterModificationGreaterThan: 90" in persistent


def test_automation_data_plane_roles_are_exact_and_parameterized() -> None:
    main = _bicep("main.bicep")
    persistent = _bicep("modules/persistent.bicep")

    assert re.search(r"param automationPrincipalId string", main)
    assert "automationPrincipalId: automationPrincipalId" in main
    assert re.search(r"param automationPrincipalId string", persistent)

    artifact_role = _resource(persistent, "automationArtifactContributor")
    assert "scope: storage" in artifact_role
    assert (
        "name: guid(storage.id, automationPrincipalId, "
        "storageBlobDataContributorRoleId)"
        in artifact_role
    )
    assert "ba92f5b4-2d11-453d-a403-e96b0029c9fe" in persistent
    assert "principalId: automationPrincipalId" in artifact_role
    assert "principalType: 'User'" in artifact_role

    push_role = _resource(persistent, "automationRegistryPush")
    assert "scope: registry" in push_role
    assert (
        "name: guid(registry.id, automationPrincipalId, acrPushRoleId)"
        in push_role
    )
    assert "8311e382-0749-4cb8-b61a-304f252e45ec" in persistent
    assert "principalId: automationPrincipalId" in push_role
    assert "principalType: 'User'" in push_role


def test_project_registry_pull_role_is_exact_and_identity_backed() -> None:
    qualification = _bicep("modules/qualification-project.bicep")

    assert re.search(r"param registryName string", qualification)
    registry = _resource(qualification, "registry")
    assert "existing" in registry
    assert "name: registryName" in registry

    pull_role = _resource(qualification, "registryPull")
    assert "scope: registry" in pull_role
    assert "name: guid(registry.id, project.id, acrPullRoleId)" in pull_role
    assert "7f951dda-4ed3-4680-a7ca-43fe172d538d" in qualification
    assert "principalId: project.identity.principalId" in pull_role
    assert "principalType: 'ServicePrincipal'" in pull_role
    assert "Microsoft.Storage" not in qualification


def test_project_registry_connection_uses_managed_identity() -> None:
    qualification = _bicep("modules/qualification-project.bicep")
    connection = _resource(qualification, "containerRegistryConnection")

    assert (
        "Microsoft.CognitiveServices/accounts/projects/connections"
        "@2025-04-01-preview"
        in connection
    )
    assert "category: 'ContainerRegistry'" in connection
    assert "target: registry.properties.loginServer" in connection
    assert "authType: 'ManagedIdentity'" in connection
    assert "isSharedToAll: false" in connection
    assert re.search(
        r"credentials:\s*\{\s*"
        r"clientId:\s*project\.identity\.principalId\s*"
        r"resourceId:\s*registry\.id\s*\}",
        connection,
    )
    assert re.search(
        r"metadata:\s*\{\s*ResourceId:\s*registry\.id\s*\}",
        connection,
    )
    assert re.search(r"dependsOn:\s*\[\s*registryPull\s*\]", connection)
    assert not re.search(
        r"^\s*output\s+",
        qualification,
        re.MULTILINE,
    )
    assert (
        qualification.count(
            "Microsoft.CognitiveServices/accounts/projects/connections"
            "@2025-04-01-preview"
        )
        == 1
    )


def test_role_assignments_do_not_hardcode_principal_ids() -> None:
    infra_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "infra").rglob("*.bicep"))
    )
    principal_values = re.findall(
        r"principalId:\s*([^\s]+)",
        infra_sources,
    )
    assert principal_values
    assert set(principal_values) == {
        "automationPrincipalId",
        "project.identity.principalId",
    }


def test_clean_dev_install_includes_otel_sdk_and_ruff() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["optional-dependencies"]["dev"]
    assert any(item.startswith("opentelemetry-sdk") for item in dependencies)
    assert any(item.startswith("ruff") for item in dependencies)
