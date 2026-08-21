from __future__ import annotations

from datetime import date

import pytest

from agent_insights_quality.runtime.azure import (
    AzureCli,
    AzureContext,
    AzureProjectManager,
    CommandResult,
    select_azure_context,
)
from agent_insights_quality.runtime.config import RuntimeConfig
from agent_insights_quality.runtime.errors import RuntimeFailure

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


def environment(*, discovery: bool = False) -> dict[str, str]:
    result = {
        "AIQ_AZURE_SUBSCRIPTION_NAME": "Qualification Subscription",
        "AIQ_TERRA_AGENT_DEPLOYMENT": "terra-test-agents",
        "AIQ_TERRA_INSIGHTS_DEPLOYMENT": "terra-insights-generator",
        "AIQ_TERRA_MODEL_VERSION": "2026-08-01",
        "AIQ_ADO_ORGANIZATION_URL": "https://ado.example.invalid",
        "AIQ_ADO_PROJECT": "Quality",
        "AIQ_ADO_TEMPLATE_ID": "template",
        "AIQ_ADO_OWNER_ID": "owner",
        "AIQ_ARTIFACT_BACKEND": "local",
        "AIQ_ARTIFACT_LOCATION": "private-artifacts",
        "AIQ_AUTOMATION_OWNER": "ninghu",
    }
    if not discovery:
        result |= {
            "AIQ_AZURE_RESOURCE_GROUP": "quality-rg",
            "AIQ_FOUNDRY_ACCOUNT": "quality-account",
            "AIQ_FOUNDRY_PROJECT": "aiq-20260820",
            "AIQ_FOUNDRY_PROJECT_ENDPOINT": "https://project.example.invalid",
            "AIQ_APPLICATION_INSIGHTS_RESOURCE_ID": (
                "/subscriptions/" + SUBSCRIPTION + "/resourceGroups/quality-rg/"
                "providers/Microsoft.Insights/components/quality"
            ),
        }
        result.pop("AIQ_AZURE_SUBSCRIPTION_NAME")
        result["AIQ_AZURE_SUBSCRIPTION_ID"] = SUBSCRIPTION
    return result


def test_runtime_config_supports_explicit_and_discovery_modes_without_repr_leaks() -> None:
    explicit = RuntimeConfig.from_env(environment())
    discovered = RuntimeConfig.from_env(environment(discovery=True))
    assert explicit.azure.subscription_id == SUBSCRIPTION
    assert discovered.azure.subscription_name == "Qualification Subscription"
    assert discovered.azure.resource_group is None
    assert "quality-rg" not in repr(explicit)
    assert all(value.startswith("sha256:") for key, value in explicit.public_summary().items() if key not in {
        "artifact_backend",
        "terra_agent_deployment",
        "terra_insights_deployment",
    })


def test_runtime_config_rejects_partial_coordinates_and_dual_subscription_selector() -> None:
    partial = environment(discovery=True)
    partial["AIQ_AZURE_RESOURCE_GROUP"] = "quality-rg"
    with pytest.raises(RuntimeFailure, match="Explicit Azure coordinates"):
        RuntimeConfig.from_env(partial)
    dual = environment()
    dual["AIQ_AZURE_SUBSCRIPTION_NAME"] = "also-set"
    with pytest.raises(RuntimeFailure, match="exactly one"):
        RuntimeConfig.from_env(dual)


class FakeAzureCli:
    def __init__(self, projects: list[dict] | None = None) -> None:
        self.projects = projects or []
        self.deleted: list[str] = []
        self.commands: list[list[str]] = []

    def run(self, arguments, **_kwargs):
        self.commands.append(list(arguments))
        return CommandResult(0, "", "")

    def json(self, arguments, **_kwargs):
        arguments = list(arguments)
        if arguments[:3] == ["account", "list", "--all"]:
            return [{"id": SUBSCRIPTION, "name": "Qualification Subscription"}]
        if arguments[:2] == ["account", "show"]:
            return {
                "id": SUBSCRIPTION,
                "tenantId": "tenant",
                "user": {"type": "user"},
            }
        if arguments[:2] == ["cloud", "show"]:
            return {"name": "AzureCloud"}
        if arguments[:3] == ["ad", "signed-in-user", "show"]:
            return {"id": "user-object"}
        if arguments[:2] == ["graph", "query"]:
            return {"data": self.projects}
        if arguments[:4] == ["cognitiveservices", "account", "deployment", "show"]:
            return {
                "properties": {
                    "model": {
                        "name": "gpt-5.6-terra",
                        "format": "OpenAI",
                        "version": "2026-08-01",
                    },
                    "provisioningState": "Succeeded",
                },
                "sku": {"capacity": 10},
            }
        if arguments[:2] == ["resource", "show"]:
            target = arguments[arguments.index("--ids") + 1]
            if "/projects/" not in target:
                if "/Microsoft.Insights/components/" in target:
                    return {
                        "id": target,
                        "type": "Microsoft.Insights/components",
                        "tags": {
                            "purpose": "agent-insights-quality",
                            "agentInsightsQualityQualification": "true",
                            "automationOwner": "ninghu",
                        },
                    }
                return {
                    "id": target,
                    "type": "Microsoft.CognitiveServices/accounts",
                    "kind": "AIServices",
                    "tags": {
                        "purpose": "agent-insights-quality",
                        "agentInsightsQualityQualification": "true",
                        "automationOwner": "ninghu",
                    },
                }
            return next(project for project in self.projects if project["id"] == target)
        raise AssertionError(arguments)

    def rest(self, method, url, body=None):
        if method == "delete":
            self.deleted.append(url)
            return None
        if method == "get" and "/connections?" in url:
            return {
                "value": [
                    {
                        "id": url.split("/connections?", 1)[0] + "/connections/application-insights",
                        "name": "application-insights",
                        "properties": {
                            "category": "AppInsights",
                            "target": (
                                "/subscriptions/" + SUBSCRIPTION
                                + "/resourceGroups/quality-rg/providers/"
                                "Microsoft.Insights/components/quality"
                            ),
                            "metadata": {
                                "purpose": "agent-insights-quality",
                                "owner_reference": "ninghu",
                                "expires_on": "2026-08-19",
                            },
                        },
                    }
                ]
            }
        raise AssertionError((method, url, body))


def project(name: str = "aiq-20260820", *, owner: str = "ninghu", expired: str = "2026-08-19") -> dict:
    project_id = (
        "/subscriptions/" + SUBSCRIPTION + "/resourceGroups/quality-rg/providers/"
        "Microsoft.CognitiveServices/accounts/quality-account/projects/" + name
    )
    return {
        "id": project_id,
        "name": name,
        "type": "microsoft.cognitiveservices/accounts/projects",
        "location": "westus2",
        "tags": {
            "purpose": "agent-insights-quality",
            "agentInsightsQualityQualification": "true",
            "automationOwner": owner,
            "expiresOn": expired,
            "reportDate": "2026-08-20",
            "catalogVersion": "sha256:catalog",
        },
        "properties": {
            "provisioningState": "Succeeded",
            "endpoint": "https://project.example.invalid",
            "applicationInsightsResourceId": (
                "/subscriptions/" + SUBSCRIPTION + "/resourceGroups/quality-rg/providers/"
                "Microsoft.Insights/components/quality"
            ),
        },
    }


def test_selects_exact_subscription_and_user_in_azure_cloud() -> None:
    cli = FakeAzureCli()
    config = RuntimeConfig.from_env(environment(discovery=True)).azure
    context = select_azure_context(cli, config)
    assert context.subscription_id == SUBSCRIPTION
    assert ["account", "set", "--subscription", SUBSCRIPTION] in cli.commands


def test_protected_tenant_and_user_must_match_when_supplied() -> None:
    values = environment(discovery=True)
    values["AIQ_AZURE_TENANT_ID"] = "22222222-2222-2222-2222-222222222222"
    config = RuntimeConfig.from_env(values).azure
    with pytest.raises(RuntimeFailure, match="tenant did not match"):
        select_azure_context(FakeAzureCli(), config)


def test_discovers_exact_owned_tagged_project_and_rejects_ambiguity() -> None:
    config = RuntimeConfig.from_env(environment(discovery=True)).azure
    cli = FakeAzureCli([project()])
    manager = AzureProjectManager(
        cli,
        AzureContext(SUBSCRIPTION, "tenant", "user"),
        config,
        "ninghu",
    )
    assert manager.discover_qualified().project_name == "aiq-20260820"
    cli.projects.append(project("aiq-20260820-r01"))
    with pytest.raises(RuntimeFailure, match="exactly one"):
        manager.discover_qualified()


def test_project_discovery_follows_resource_graph_pages() -> None:
    class PagedAzureCli(FakeAzureCli):
        def json(self, arguments, **kwargs):
            arguments = list(arguments)
            if arguments[:2] == ["graph", "query"]:
                if "--skip-token" not in arguments:
                    return {"data": [project(owner="other")], "skipToken": "next-page"}
                return {"data": [project()]}
            return super().json(arguments, **kwargs)

    config = RuntimeConfig.from_env(environment(discovery=True)).azure
    manager = AzureProjectManager(
        PagedAzureCli([project()]),
        AzureContext(SUBSCRIPTION, "tenant", "user"),
        config,
        "ninghu",
    )
    assert manager.discover_qualified().project_name == "aiq-20260820"


def test_project_cleanup_deletes_only_exact_owned_expired_projects() -> None:
    config = RuntimeConfig.from_env(environment(discovery=True)).azure
    cli = FakeAzureCli(
        [
            project(),
            project("customer-project"),
            project("aiq-20260820-r01", owner="someone-else"),
            project("aiq-20260821-r01", expired="2026-08-30"),
        ]
    )
    manager = AzureProjectManager(cli, AzureContext(SUBSCRIPTION, "tenant", "user"), config, "ninghu")
    assert manager.cleanup_expired(now=date(2026, 8, 21), dry_run=False) == ["aiq-20260820"]
    assert len(cli.deleted) == 1


def test_project_reuse_requires_exact_date_catalog_and_ownership() -> None:
    config = RuntimeConfig.from_env(environment()).azure
    existing = project()
    cli = FakeAzureCli([existing])
    manager = AzureProjectManager(cli, AzureContext(SUBSCRIPTION, "tenant", "user"), config, "ninghu")
    selected = manager.select_or_create(date(2026, 8, 20), "sha256:catalog")
    assert selected.project_name == "aiq-20260820"


def test_azure_cli_rejects_argument_injection_without_invoking_executor() -> None:
    invoked = False

    def executor(_command, _timeout):
        nonlocal invoked
        invoked = True
        return CommandResult(0, "", "")

    with pytest.raises(RuntimeFailure, match="null byte"):
        AzureCli(executor).run(["resource", "show", "--ids", "bad\x00value"])
    assert invoked is False
