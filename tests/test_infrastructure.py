from __future__ import annotations

import re

from agent_insights_quality.contracts import ROOT


def _bicep(path: str) -> str:
    return (ROOT / "infra" / path).read_text(encoding="utf-8")


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
    assert qualification.count("principalId: project.identity.principalId") == 2

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
