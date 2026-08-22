from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import agent_insights_quality.cli as cli_module
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
        "AIQ_MONITOR_OWNERSHIP_RECEIPT": "private-state/monitors.json",
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


def test_runtime_config_accepts_azure_subscription_display_name_with_ampersand() -> None:
    source = environment(discovery=True)
    source["AIQ_AZURE_SUBSCRIPTION_NAME"] = "AML - Experiences R&D"
    assert RuntimeConfig.from_env(source).azure.subscription_name == "AML - Experiences R&D"


@pytest.mark.parametrize(
    "subscription_name",
    [
        " AML - Experiences R&D",
        "AML - Experiences R&D ",
        "AML\nExperiences",
        "AML\tExperiences",
        "AML/Experiences",
        r"AML\Experiences",
        "https://example.invalid",
        "AML?environment=prod",
        "AML#fragment",
        "AML&environment=prod",
        "A" * 128,
    ],
)
def test_runtime_config_rejects_unsafe_azure_subscription_display_names(
    subscription_name: str,
) -> None:
    source = environment(discovery=True)
    source["AIQ_AZURE_SUBSCRIPTION_NAME"] = subscription_name
    with pytest.raises(RuntimeFailure, match="subscription display name"):
        RuntimeConfig.from_env(source)


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
    def __init__(
        self,
        projects: list[dict] | None = None,
        *,
        roles: bool = True,
        role_create_failure: bool = False,
    ) -> None:
        self.projects = projects or []
        self.deleted: list[str] = []
        self.commands: list[list[str]] = []
        self.roles = roles
        self.role_create_failure = role_create_failure

    def run(self, arguments, **_kwargs):
        self.commands.append(list(arguments))
        if arguments[:3] == ["role", "assignment", "create"] and self.role_create_failure:
            return CommandResult(1, "", "forbidden")
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
        if arguments[:3] == ["role", "assignment", "list"]:
            if not self.roles:
                return []
            scope = arguments[arguments.index("--scope") + 1]
            role = (
                "43d0d8ad-25c7-4714-9337-8ba259a9fe05"
                if "microsoft.insights/components" in scope.casefold()
                else "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
            )
            return [{"scope": scope, "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/" + role}]
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
        if method == "put" and "/connections/" in url:
            return body
        if method == "get" and "/connections?" in url:
            return {
                "value": [
                    {
                        "id": url.split("/connections?", 1)[0] + "/connections/application-insights",
                        "name": "application-insights",
                        "etag": '"connection-etag"',
                        "properties": {
                            "category": "AppInsights",
                            "authType": "ApiKey",
                            "target": (
                                "/subscriptions/" + SUBSCRIPTION
                                + "/resourceGroups/quality-rg/providers/"
                                "Microsoft.Insights/components/quality"
                            ),
                            "metadata": {
                                "ApiType": "Azure",
                                "ResourceId": (
                                    "/subscriptions/" + SUBSCRIPTION
                                    + "/resourceGroups/quality-rg/providers/"
                                    "Microsoft.Insights/components/quality"
                                ),
                                "purpose": "agent-insights-quality",
                                "owner_reference": "ninghu",
                                "expires_on": "2026-08-19",
                            },
                        },
                    }
                ]
            }
        raise AssertionError((method, url, body))

    def put_if_absent(self, url, body):
        name = url.split("/projects/", 1)[1].split("?", 1)[0]
        created = project(name)
        created["tags"] = dict(body["tags"])
        self.projects.append(created)
        return True

    def put_conditionally(self, url, body, *, etag):
        return True


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
        "identity": {"type": "SystemAssigned", "principalId": "project-principal"},
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


def test_project_cleanup_continues_after_an_individual_delete_failure() -> None:
    class PartiallyFailingAzureCli(FakeAzureCli):
        def rest(self, method, url, body=None):
            if method == "delete" and "/aiq-20260819?" in url:
                raise RuntimeFailure("delete_failed", "Synthetic delete failure.")
            return super().rest(method, url, body)

    cli = PartiallyFailingAzureCli(
        [project("aiq-20260819"), project("aiq-20260820")]
    )
    config = RuntimeConfig.from_env(environment(discovery=True)).azure
    manager = AzureProjectManager(cli, AzureContext(SUBSCRIPTION, "tenant", "user"), config, "ninghu")
    with pytest.raises(RuntimeFailure, match="other eligible projects were processed") as caught:
        manager.cleanup_expired(now=date(2026, 8, 21), dry_run=False)
    assert caught.value.details == {"deleted_count": 1, "failure_count": 1}
    assert len(cli.deleted) == 1


def test_project_reuse_requires_exact_date_catalog_and_ownership() -> None:
    config = RuntimeConfig.from_env(environment()).azure
    existing = project()
    cli = FakeAzureCli([existing])
    manager = AzureProjectManager(cli, AzureContext(SUBSCRIPTION, "tenant", "user"), config, "ninghu")
    selected = manager.select_or_create(date(2026, 8, 20), "sha256:catalog")
    assert selected.project_name == "aiq-20260820"


def test_preprovisioned_api_key_connection_is_reconciled_without_secret_access() -> None:
    class NoConnectionWritesAzureCli(FakeAzureCli):
        def put_conditionally(self, url, body, *, etag):
            raise AssertionError("Runtime must not write the ApiKey connection")

    config = RuntimeConfig.from_env(environment()).azure
    manager = AzureProjectManager(
        NoConnectionWritesAzureCli([project()]),
        AzureContext(SUBSCRIPTION, "tenant", "user"),
        config,
        "ninghu",
    )

    assert (
        manager.select_or_create(date(2026, 8, 20), "sha256:catalog").project_name
        == "aiq-20260820"
    )


@pytest.mark.parametrize(
    ("metadata_matches", "has_pull_role", "expected_error"),
    [
        (True, True, None),
        (False, True, "project_connection_conflict"),
        (True, False, "project_role_assignments_missing"),
    ],
)
def test_acr_image_accepts_redacted_credentials_but_requires_visible_binding(
    metadata_matches: bool,
    has_pull_role: bool,
    expected_error: str | None,
) -> None:
    registry_id = (
        "/subscriptions/" + SUBSCRIPTION + "/resourceGroups/quality-rg/providers/"
        "Microsoft.ContainerRegistry/registries/aiqacr123"
    )

    class RegistryAzureCli(FakeAzureCli):
        def rest(self, method, url, body=None):
            if method == "get" and "/connections?" in url:
                app = super().rest(method, url, body)["value"][0]
                project_id = url.split("/connections?", 1)[0]
                return {
                    "value": [
                        app,
                        {
                            "id": project_id + "/connections/container-registry",
                            "name": "container-registry",
                            "properties": {
                                "category": "ContainerRegistry",
                                "target": "aiqacr123.azurecr.io",
                                "authType": "ManagedIdentity",
                                "isSharedToAll": False,
                                "credentials": None,
                                "metadata": {
                                    "ResourceId": (
                                        registry_id
                                        if metadata_matches
                                        else registry_id + "-other"
                                    )
                                },
                            },
                        },
                    ]
                }
            return super().rest(method, url, body)

        def json(self, arguments, **kwargs):
            arguments = list(arguments)
            if arguments[:2] == ["resource", "show"]:
                target = arguments[arguments.index("--ids") + 1]
                if target.casefold() == registry_id.casefold():
                    return {
                        "id": registry_id,
                        "type": "Microsoft.ContainerRegistry/registries",
                        "tags": {
                            "purpose": "agent-insights-quality",
                            "agentInsightsQualityQualification": "true",
                            "automationOwner": "ninghu",
                        },
                    }
            if arguments[:3] == ["role", "assignment", "list"]:
                scope = arguments[arguments.index("--scope") + 1]
                if scope.casefold() == registry_id.casefold():
                    if not has_pull_role:
                        return []
                    return [
                        {
                            "scope": registry_id,
                            "roleDefinitionId": (
                                "/providers/Microsoft.Authorization/roleDefinitions/"
                                "7f951dda-4ed3-4680-a7ca-43fe172d538d"
                            ),
                        }
                    ]
            return super().json(arguments, **kwargs)

    source = environment()
    source["AIQ_TICKET_IMAGE_URI"] = (
        "aiqacr123.azurecr.io/ticket@sha256:" + ("a" * 64)
    )
    config = RuntimeConfig.from_env(source).azure
    manager = AzureProjectManager(
        RegistryAzureCli([project()]),
        AzureContext(SUBSCRIPTION, "tenant", "user"),
        config,
        "ninghu",
    )

    if expected_error is None:
        assert (
            manager.select_or_create(
                date(2026, 8, 20),
                "sha256:catalog",
            ).project_name
            == "aiq-20260820"
        )
    else:
        with pytest.raises(RuntimeFailure) as caught:
            manager.select_or_create(date(2026, 8, 20), "sha256:catalog")
        assert caught.value.code == expected_error


def test_acr_image_rejects_missing_registry_connection() -> None:
    source = environment()
    source["AIQ_TICKET_IMAGE_URI"] = (
        "aiqacr123.azurecr.io/ticket@sha256:" + ("a" * 64)
    )
    config = RuntimeConfig.from_env(source).azure
    manager = AzureProjectManager(
        FakeAzureCli([project()]),
        AzureContext(SUBSCRIPTION, "tenant", "user"),
        config,
        "ninghu",
    )

    with pytest.raises(RuntimeFailure, match="ContainerRegistry") as caught:
        manager.select_or_create(date(2026, 8, 20), "sha256:catalog")
    assert caught.value.code == "missing_project_connections"


def test_project_preflight_requires_exact_managed_identity_roles() -> None:
    config = RuntimeConfig.from_env(environment(discovery=True)).azure
    manager = AzureProjectManager(
        FakeAzureCli([project()], roles=False),
        AzureContext(SUBSCRIPTION, "tenant", "user"),
        config,
        "ninghu",
    )
    with pytest.raises(RuntimeFailure, match="lacks required"):
        manager.discover_qualified()


def test_explicit_project_validation_does_not_depend_on_discovery_tags() -> None:
    explicit = project()
    explicit["tags"] = {}

    class UntaggedExplicitCli(FakeAzureCli):
        def json(self, arguments, **kwargs):
            arguments = list(arguments)
            if arguments[:2] == ["resource", "show"]:
                target = arguments[arguments.index("--ids") + 1]
                if "/projects/" not in target:
                    result = dict(super().json(arguments, **kwargs))
                    result["tags"] = {}
                    return result
            return super().json(arguments, **kwargs)

    config = RuntimeConfig.from_env(environment()).azure
    manager = AzureProjectManager(
        UntaggedExplicitCli([explicit]),
        AzureContext(SUBSCRIPTION, "tenant", "user"),
        config,
        "ninghu",
    )
    assert manager.validate_explicit_project().project_name == "aiq-20260820"


def test_project_creation_rejects_a_foreign_raced_name() -> None:
    class RacingAzureCli(FakeAzureCli):
        def __init__(self):
            super().__init__([])
            self.attempts: list[str] = []

        def put_if_absent(self, url, body):
            name = url.split("/projects/", 1)[1].split("?", 1)[0]
            self.attempts.append(name)
            if name == "aiq-20260820":
                self.projects.append(project(name, owner="other"))
                return False
            return super().put_if_absent(url, body)

    cli = RacingAzureCli()
    config = RuntimeConfig.from_env(environment()).azure
    manager = AzureProjectManager(cli, AzureContext(SUBSCRIPTION, "tenant", "user"), config, "ninghu")
    with pytest.raises(RuntimeFailure) as caught:
        manager.select_or_create(date(2026, 8, 20), "sha256:catalog")
    assert caught.value.code == "ownership_mismatch"
    assert cli.attempts == ["aiq-20260820"]


def test_stale_resource_graph_conflict_reuses_exact_owned_project() -> None:
    class StaleGraphAzureCli(FakeAzureCli):
        def __init__(self):
            super().__init__([project()])
            self.attempts: list[str] = []

        def json(self, arguments, **kwargs):
            if list(arguments)[:2] == ["graph", "query"]:
                return {"data": []}
            return super().json(arguments, **kwargs)

        def put_if_absent(self, url, body):
            self.attempts.append(url.split("/projects/", 1)[1].split("?", 1)[0])
            return False

    cli = StaleGraphAzureCli()
    config = RuntimeConfig.from_env(environment()).azure
    manager = AzureProjectManager(cli, AzureContext(SUBSCRIPTION, "tenant", "user"), config, "ninghu")

    selected = manager.select_or_create(date(2026, 8, 20), "sha256:catalog")

    assert selected.project_name == "aiq-20260820"
    assert cli.attempts == ["aiq-20260820"]


def test_project_reconciliation_reports_role_assignment_permission_blocker() -> None:
    cli = FakeAzureCli([project()], roles=False, role_create_failure=True)
    config = RuntimeConfig.from_env(environment()).azure
    manager = AzureProjectManager(cli, AzureContext(SUBSCRIPTION, "tenant", "user"), config, "ninghu")
    with pytest.raises(RuntimeFailure, match="roleAssignments/write") as caught:
        manager.select_or_create(date(2026, 8, 20), "sha256:catalog")
    assert caught.value.code == "role_assignment_permission_blocked"


def test_project_creation_fails_closed_when_api_key_connection_is_missing() -> None:
    class MissingConnectionAzureCli(FakeAzureCli):
        def __init__(self):
            super().__init__([])
            self.connection_puts = 0

        def rest(self, method, url, body=None):
            if method == "get" and "/connections?" in url:
                return {"value": []}
            return super().rest(method, url, body)

        def put_conditionally(self, url, body, *, etag):
            self.connection_puts += 1
            return super().put_conditionally(url, body, etag=etag)

    cli = MissingConnectionAzureCli()
    config = RuntimeConfig.from_env(environment()).azure
    manager = AzureProjectManager(
        cli,
        AzureContext(SUBSCRIPTION, "tenant", "user"),
        config,
        "ninghu",
    )

    with pytest.raises(RuntimeFailure, match="preprovisioned ApiKey") as blocked:
        manager.select_or_create(date(2026, 8, 20), "sha256:catalog")
    assert blocked.value.code == "missing_project_connections"
    assert cli.connection_puts == 0


def test_stale_aad_connection_is_rejected_without_overwrite() -> None:
    class ReplacedConnectionAzureCli(FakeAzureCli):
        def __init__(self):
            super().__init__([project()])
            self.connection = self._connection(
                owner="ninghu",
                etag='"owned-etag"',
                expires_on="2026-08-18",
            )
            self.writes = 0

        @staticmethod
        def _connection(*, owner, etag, expires_on="2026-08-19"):
            project_id = project()["id"]
            return {
                "id": project_id + "/connections/application-insights",
                "name": "application-insights",
                "etag": etag,
                "properties": {
                    "category": "AppInsights",
                    "target": (
                        "/subscriptions/" + SUBSCRIPTION
                        + "/resourceGroups/quality-rg/providers/"
                        "Microsoft.Insights/components/quality"
                    ),
                    "authType": "AAD",
                    "metadata": {
                        "purpose": "agent-insights-quality",
                        "owner_reference": owner,
                        "expires_on": expires_on,
                    },
                },
            }

        def rest(self, method, url, body=None):
            if method == "get" and "/connections?" in url:
                return {"value": [self.connection]}
            return super().rest(method, url, body)

        def put_conditionally(self, url, body, *, etag):
            self.writes += 1
            assert etag == '"owned-etag"'
            self.connection = self._connection(owner="foreign-owner", etag='"foreign-etag"')
            return False

    cli = ReplacedConnectionAzureCli()
    config = RuntimeConfig.from_env(environment()).azure
    manager = AzureProjectManager(cli, AzureContext(SUBSCRIPTION, "tenant", "user"), config, "ninghu")

    with pytest.raises(RuntimeFailure) as caught:
        manager.select_or_create(date(2026, 8, 20), "sha256:catalog")

    assert caught.value.code == "project_connection_conflict"
    assert cli.writes == 0
    assert cli.connection["properties"]["authType"] == "AAD"


def test_azure_cli_rejects_argument_injection_without_invoking_executor() -> None:
    invoked = False

    def executor(_command, _timeout):
        nonlocal invoked
        invoked = True
        return CommandResult(0, "", "")

    with pytest.raises(RuntimeFailure, match="null byte"):
        AzureCli(executor).run(["resource", "show", "--ids", "bad\x00value"])
    assert invoked is False


@pytest.mark.parametrize(
    "stderr",
    [
        'ERROR: Precondition Failed({"error":{"code":"PreconditionFailed","message":"ETag mismatch"}})',
        'ERROR: Conflict({"error":{"code":"Conflict","message":"Resource exists"}})',
        '{"error":{"code":"PreconditionFailed","message":"ETag mismatch"}}',
        "HTTP 412 Precondition Failed",
        "status code: 409 Conflict",
    ],
)
def test_azure_cli_recognizes_exact_conditional_failures(stderr: str) -> None:
    commands: list[list[str]] = []

    def executor(command, _timeout):
        commands.append(list(command))
        return CommandResult(1, "", stderr)

    cli = AzureCli(executor)
    assert cli.put_conditionally("resource?api-version=test", {}, etag=None) is False
    assert "If-None-Match=*" in commands[-1]
    assert cli.put_conditionally(
        "resource?api-version=test",
        {},
        etag='"owned-etag"',
    ) is False
    assert 'If-Match="owned-etag"' in commands[-1]


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: AuthorizationFailed({'message':'Conflict while checking permissions'})",
        "The request had 412 validation findings.",
        "ERROR: ConflictResolutionRequired({})",
        '{"error":{"code":"AuthorizationFailed","message":"Precondition Failed"}}',
        "ERROR: Precondition Failed to parse response",
    ],
)
def test_azure_cli_does_not_misclassify_non_conflict_failures(stderr: str) -> None:
    cli = AzureCli(lambda _command, _timeout: CommandResult(1, "", stderr))
    with pytest.raises(RuntimeFailure) as caught:
        cli.put_conditionally("resource?api-version=test", {}, etag=None)
    assert caught.value.code == "azure_conditional_write_blocked"


def test_azure_cli_rejects_etag_header_injection() -> None:
    cli = AzureCli(lambda _command, _timeout: CommandResult(0, "{}", ""))
    with pytest.raises(RuntimeFailure, match="ETag"):
        cli.put_conditionally(
            "resource?api-version=test",
            {},
            etag='"safe"\r\nIf-Match=*',
        )


def test_cleanup_cli_processes_every_resource_class_before_reporting_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Projects:
        def qualified_projects(self, failures):
            calls.append("monitors")
            failures.append("partial_project")
            return []

        def cleanup_owned_connections(self, *_args, **_kwargs):
            calls.append("connections")
            raise RuntimeFailure("connection_cleanup_failed", "Synthetic failure.")

        def cleanup_expired(self, **_kwargs):
            calls.append("projects")
            return []

    class Artifacts:
        def cleanup_expired(self, *_args, **_kwargs):
            calls.append("artifacts")
            raise RuntimeFailure("artifact_cleanup_failed", "Synthetic failure.")

    config = SimpleNamespace(
        artifacts=SimpleNamespace(backend="local", location="private", container=None),
        automation_owner="ninghu",
        monitor_ownership_receipt="private/monitors.json",
    )
    monkeypatch.setattr(cli_module.RuntimeConfig, "from_env", lambda: config)
    monkeypatch.setattr(cli_module, "_runtime_context", lambda _config: (object(), Projects()))
    monkeypatch.setattr(cli_module, "LocalArtifactStore", lambda _path: Artifacts())
    assert cli_module.main(["cleanup"]) == 1
    assert calls == ["monitors", "connections", "artifacts", "projects"]


@pytest.mark.parametrize(
    ("values", "arguments", "expected"),
    [
        (environment(), ["preflight"], "explicit"),
        (environment(discovery=True), ["preflight"], "discovery"),
        (environment(), ["preflight", "--discover-project"], "discovery"),
    ],
)
def test_preflight_routes_explicit_and_discovery_modes(
    values: dict[str, str],
    arguments: list[str],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    config = RuntimeConfig.from_env(values)
    selected = SimpleNamespace(
        project_id=project()["id"],
        project_endpoint="https://project.example.invalid",
    )

    class Projects:
        def discover_qualified(self):
            calls.append("discovery")
            return selected

        def validate_explicit_project(self):
            calls.append("explicit")
            return selected

    class Insights:
        def __init__(self, endpoint, _credential):
            assert endpoint == selected.project_endpoint

        def probe(self):
            calls.append("probe")

    monkeypatch.setattr(cli_module.RuntimeConfig, "from_env", lambda: config)
    monkeypatch.setattr(
        cli_module,
        "_runtime_context",
        lambda _config: (SimpleNamespace(), Projects()),
    )
    monkeypatch.setattr(cli_module, "AgentInsightsClient", Insights)

    assert cli_module.main(arguments) == 0
    assert calls == [expected, "probe"]
