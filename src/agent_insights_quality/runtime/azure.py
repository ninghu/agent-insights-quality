from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .config import AzureRuntimeConfig
from .errors import RuntimeFailure

_PROJECT_NAME = re.compile(r"^aiq-[0-9]{8}(?:-r[0-9]{2})?$")
_PROJECT_TYPE = "microsoft.cognitiveservices/accounts/projects"
_PURPOSE_TAG = "agent-insights-quality"
_QUALIFICATION_TAG = "true"
_MONITORING_READER_ROLE = "43d0d8ad-25c7-4714-9337-8ba259a9fe05"
_MODEL_INFERENCE_ROLE = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Executor = Callable[[Sequence[str], float], CommandResult]


def _execute(command: Sequence[str], timeout: float) -> CommandResult:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise RuntimeFailure("azure_cli_missing", "Azure CLI is not installed.") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeFailure(
            "azure_cli_timeout",
            "Azure CLI exceeded its bounded timeout.",
            {"timeout_seconds": timeout},
            transient=True,
        ) from error
    return CommandResult(result.returncode, result.stdout, result.stderr)


class AzureCli:
    """Small injectable Azure CLI boundary that never invokes a shell."""

    def __init__(self, executor: Executor | None = None) -> None:
        self._executor = executor or _execute
        self._executable = "az" if executor else (shutil.which("az.cmd" if os.name == "nt" else "az") or "az")

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout: float = 120,
        allow_failure: bool = False,
    ) -> CommandResult:
        if any("\x00" in argument for argument in arguments):
            raise RuntimeFailure("invalid_azure_argument", "Azure CLI argument contains a null byte.")
        result = self._executor([self._executable, *arguments], timeout)
        if result.returncode and not allow_failure:
            raise RuntimeFailure(
                "azure_cli_failed",
                "Azure CLI command failed.",
                {
                    "command": ["az", *self._safe_arguments(arguments)],
                    "returncode": result.returncode,
                },
            )
        return result

    def json(
        self,
        arguments: Sequence[str],
        *,
        timeout: float = 120,
        allow_failure: bool = False,
        allow_empty: bool = False,
    ) -> Any:
        result = self.run([*arguments, "--output", "json"], timeout=timeout, allow_failure=allow_failure)
        if result.returncode and allow_failure:
            return None
        if allow_empty and not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeFailure("invalid_azure_response", "Azure CLI returned invalid JSON.") from error

    def rest(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if method.casefold() not in {"get", "put", "patch", "delete"}:
            raise RuntimeFailure("invalid_azure_method", "Unsupported Azure REST method.")
        arguments = ["rest", "--method", method, "--url", url]
        if body is not None:
            arguments.extend(["--body", json.dumps(body, separators=(",", ":"))])
        if headers:
            arguments.append("--headers")
            arguments.extend(f"{key}={value}" for key, value in headers.items())
        return self.json(
            arguments,
            allow_empty=method.casefold() == "delete",
        )

    def put_if_absent(self, url: str, body: Mapping[str, Any]) -> bool:
        arguments = [
            "rest",
            "--method",
            "put",
            "--url",
            url,
            "--headers",
            "If-None-Match=*",
            "--body",
            json.dumps(body, separators=(",", ":")),
            "--output",
            "json",
        ]
        result = self.run(arguments, allow_failure=True)
        if result.returncode == 0:
            return True
        if re.search(r"\b(?:409|412)\b", result.stderr):
            return False
        raise RuntimeFailure(
            "azure_project_create_blocked",
            "Conditional project creation failed. Verify project write permission on the exact "
            "Foundry account; the runtime will not overwrite an existing project.",
        )

    @staticmethod
    def _safe_arguments(arguments: Sequence[str]) -> list[str]:
        safe: list[str] = []
        hide = False
        for argument in arguments:
            safe.append("******" if hide else argument)
            hide = argument.casefold() in {"--body", "--headers", "--password", "--client-secret"}
        return safe


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    expires_on: int


class AzureCliCredential:
    def __init__(self, cli: AzureCli) -> None:
        self._cli = cli

    def get_token(self, *scopes: str) -> AccessToken:
        if len(scopes) != 1 or not scopes[0].endswith("/.default"):
            raise RuntimeFailure("invalid_token_scope", "Exactly one Azure default scope is required.")
        resource = scopes[0].removesuffix("/.default")
        payload = _mapping(
            self._cli.json(["account", "get-access-token", "--resource", resource]),
            "invalid_access_token",
            "Azure CLI access token response was invalid.",
        )
        token = str(payload.get("accessToken") or "")
        if not token:
            raise RuntimeFailure("invalid_access_token", "Azure CLI returned an empty access token.")
        raw_expiry = payload.get("expires_on") or payload.get("expiresOn")
        try:
            expires_on = int(raw_expiry)
        except (TypeError, ValueError):
            expires_on = int(time.time()) + 300
        return AccessToken(token, expires_on)


@dataclass(frozen=True, slots=True, repr=False)
class AzureContext:
    subscription_id: str
    tenant_id: str
    user_object_id: str


@dataclass(frozen=True, slots=True, repr=False)
class ProjectResources:
    project_id: str
    project_name: str
    account_name: str
    resource_group: str
    project_endpoint: str
    application_insights_resource_id: str
    principal_id: str
    managed: bool
    tags: Mapping[str, str]


def _mapping(value: Any, code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeFailure(code, message)
    return value


def _items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping) and "data" in value:
        raw = value.get("data")
    elif isinstance(value, Mapping) and "value" in value:
        raw = value.get("value")
    else:
        raw = value
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise RuntimeFailure("invalid_azure_response", "Azure resource list response was invalid.")
    return list(raw)


def select_azure_context(cli: AzureCli, config: AzureRuntimeConfig) -> AzureContext:
    subscriptions = _items(cli.json(["account", "list", "--all"]))
    matches = [
        item
        for item in subscriptions
        if (
            config.subscription_id is not None
            and str(item.get("id") or "").casefold() == config.subscription_id.casefold()
        )
        or (
            config.subscription_name is not None
            and str(item.get("name") or "") == config.subscription_name
        )
    ]
    if len(matches) != 1:
        raise RuntimeFailure(
            "subscription_selection_failed",
            "Subscription selector did not resolve exactly one accessible subscription.",
        )
    subscription_id = str(matches[0].get("id") or "")
    cli.run(["account", "set", "--subscription", subscription_id])
    active = _mapping(
        cli.json(["account", "show"]),
        "invalid_azure_context",
        "Azure account context was invalid.",
    )
    cloud = _mapping(
        cli.json(["cloud", "show"]),
        "invalid_azure_context",
        "Azure cloud context was invalid.",
    )
    if str(cloud.get("name") or "") != "AzureCloud":
        raise RuntimeFailure("unsupported_azure_cloud", "The qualification runtime requires AzureCloud.")
    if str(active.get("id") or "").casefold() != subscription_id.casefold():
        raise RuntimeFailure("subscription_selection_failed", "Azure CLI did not select the exact subscription.")
    user = active.get("user")
    if not isinstance(user, Mapping) or str(user.get("type") or "").casefold() != "user":
        raise RuntimeFailure("invalid_azure_identity", "An authenticated Azure user identity is required.")
    signed_in = _mapping(
        cli.json(["ad", "signed-in-user", "show"]),
        "invalid_azure_identity",
        "The signed-in Azure user could not be resolved.",
    )
    tenant_id = str(active.get("tenantId") or "")
    user_object_id = str(signed_in.get("id") or signed_in.get("objectId") or "")
    if config.expected_tenant_id and tenant_id.casefold() != config.expected_tenant_id.casefold():
        raise RuntimeFailure("invalid_azure_identity", "Azure tenant did not match protected configuration.")
    if (
        config.expected_user_object_id
        and user_object_id.casefold() != config.expected_user_object_id.casefold()
    ):
        raise RuntimeFailure("invalid_azure_identity", "Azure user did not match protected configuration.")
    return AzureContext(
        subscription_id=subscription_id,
        tenant_id=tenant_id,
        user_object_id=user_object_id,
    )


class AzureProjectManager:
    def __init__(self, cli: AzureCli, context: AzureContext, config: AzureRuntimeConfig, owner: str) -> None:
        self._cli = cli
        self._context = context
        self._config = config
        self._owner = owner

    def _projects(self) -> list[Mapping[str, Any]]:
        query = (
            "Resources | where type =~ 'microsoft.cognitiveservices/accounts/projects' "
            "| project id,name,type,location,tags,identity,properties"
        )
        results: list[Mapping[str, Any]] = []
        skip_token = ""
        while True:
            arguments = [
                "graph",
                "query",
                "--subscriptions",
                self._context.subscription_id,
                "--graph-query",
                query,
                "--first",
                "1000",
            ]
            if skip_token:
                arguments.extend(["--skip-token", skip_token])
            payload = self._cli.json(arguments)
            results.extend(_items(payload))
            skip_token = (
                str(payload.get("skipToken") or payload.get("skip_token") or "")
                if isinstance(payload, Mapping)
                else ""
            )
            if not skip_token:
                return results

    def _owned(self, item: Mapping[str, Any]) -> bool:
        tags = item.get("tags")
        return (
            str(item.get("type") or "").casefold() == _PROJECT_TYPE
            and isinstance(tags, Mapping)
            and str(tags.get("purpose") or "") == _PURPOSE_TAG
            and str(tags.get("agentInsightsQualityQualification") or "").casefold()
            == _QUALIFICATION_TAG
            and str(tags.get("automationOwner") or "") == self._owner
        )

    def discover_qualified(self) -> ProjectResources:
        matches = [item for item in self._projects() if self._owned(item)]
        selected_name = self._config.fallback_project_name or self._config.project_name
        if selected_name:
            matches = [
                item for item in matches if item.get("name") == selected_name
            ]
        if len(matches) != 1:
            raise RuntimeFailure(
                "qualified_project_discovery_failed",
                "Discovery did not resolve exactly one owned qualification project.",
                {"match_count": len(matches)},
            )
        return self._validate_project(matches[0], managed=False)

    def qualified_projects(self, failures: list[str]) -> list[ProjectResources]:
        selected: list[ProjectResources] = []
        for item in self._projects():
            if not self._owned(item):
                continue
            try:
                selected.append(self._validate_project(item, managed=False))
            except RuntimeFailure as error:
                failures.append(error.code)
                continue
        return selected

    def _validate_project(self, item: Mapping[str, Any], *, managed: bool) -> ProjectResources:
        project_id = str(item.get("id") or "")
        parts = project_id.strip("/").split("/")
        try:
            group = parts[parts.index("resourceGroups") + 1]
            account = parts[parts.index("accounts") + 1]
        except (ValueError, IndexError) as error:
            raise RuntimeFailure("invalid_project_resource", "Project resource ID shape was invalid.") from error
        if str(item.get("location") or "").casefold().replace(" ", "") != "westus2":
            raise RuntimeFailure("invalid_project_region", "Qualification project must be in westus2.")
        account_id = project_id.rsplit("/projects/", 1)[0]
        account_resource = _mapping(
            self._cli.json(["resource", "show", "--ids", account_id]),
            "invalid_foundry_account",
            "Foundry account response was invalid.",
        )
        if (
            str(account_resource.get("type") or "").casefold()
            != "microsoft.cognitiveservices/accounts"
            or str(account_resource.get("kind") or "").casefold() != "aiservices"
            or not self._owned_tags(account_resource.get("tags"))
        ):
            raise RuntimeFailure(
                "invalid_foundry_account",
                "Project parent is not an Azure AI Services account.",
            )
        properties = item.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        identity = item.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        principal_id = str(identity.get("principalId") or "")
        if not principal_id:
            raise RuntimeFailure(
                "project_identity_unavailable",
                "Project managed identity is not ready.",
                transient=True,
            )
        account_properties = account_resource.get("properties")
        account_properties = account_properties if isinstance(account_properties, Mapping) else {}
        account_endpoints = account_properties.get("endpoints")
        account_endpoints = account_endpoints if isinstance(account_endpoints, Mapping) else {}
        account_endpoint = str(
            account_endpoints.get("AI Foundry API")
            or account_endpoints.get("Azure AI Foundry")
            or account_properties.get("endpoint")
            or ""
        ).rstrip("/")
        endpoint = str(properties.get("endpoint") or self._config.project_endpoint or "")
        if not endpoint and account_endpoint:
            endpoint = (
                f"{account_endpoint}/api/projects/"
                + urllib.parse.quote(str(item.get("name") or ""), safe="")
            )
        app_insights = self._application_insights_connection(project_id)
        app_insights_resource = _mapping(
            self._cli.json(["resource", "show", "--ids", app_insights]),
            "invalid_project_connections",
            "Application Insights resource response was invalid.",
        )
        if (
            str(app_insights_resource.get("type") or "").casefold()
            != "microsoft.insights/components"
            or not self._owned_tags(app_insights_resource.get("tags"))
        ):
            raise RuntimeFailure(
                "invalid_project_connections",
                "Application Insights component is not exactly owned by this runtime.",
            )
        if not endpoint.startswith("https://") or not app_insights.startswith(
            f"/subscriptions/{self._context.subscription_id}/"
        ):
            raise RuntimeFailure(
                "invalid_project_connections",
                "Project endpoint or Application Insights connection was not safely resolved.",
            )
        if self._config.resource_group and group != self._config.resource_group:
            raise RuntimeFailure("project_selection_mismatch", "Discovered project resource group differs from configuration.")
        if self._config.account_name and account != self._config.account_name:
            raise RuntimeFailure("project_selection_mismatch", "Discovered Foundry account differs from configuration.")
        self._validate_terra(account, group)
        self._validate_project_roles(
            principal_id=principal_id,
            account_id=account_id,
            application_insights_id=app_insights,
        )
        self._cli.json(["resource", "show", "--ids", project_id])
        tags = item.get("tags")
        return ProjectResources(
            project_id=project_id,
            project_name=str(item.get("name") or ""),
            account_name=account,
            resource_group=group,
            project_endpoint=endpoint.rstrip("/"),
            application_insights_resource_id=app_insights,
            principal_id=principal_id,
            managed=managed,
            tags={str(key): str(value) for key, value in tags.items()} if isinstance(tags, Mapping) else {},
        )

    def _validate_project_roles(
        self,
        *,
        principal_id: str,
        account_id: str,
        application_insights_id: str,
    ) -> None:
        missing = self._missing_project_roles(
            principal_id=principal_id,
            account_id=account_id,
            application_insights_id=application_insights_id,
        )
        if missing:
            raise RuntimeFailure(
                "project_role_assignments_missing",
                "Project identity lacks required App Insights read or model inference roles. "
                "Grant the exact roles or run deployment with roleAssignments/write permission.",
                {"missing_role_count": len(missing)},
            )

    def _missing_project_roles(
        self,
        *,
        principal_id: str,
        account_id: str,
        application_insights_id: str,
    ) -> list[tuple[str, str]]:
        required = (
            (application_insights_id, _MONITORING_READER_ROLE),
            (account_id, _MODEL_INFERENCE_ROLE),
        )
        observed: set[tuple[str, str]] = set()
        for scope, _ in required:
            assignments = _items(
                self._cli.json(
                    [
                        "role",
                        "assignment",
                        "list",
                        "--assignee-object-id",
                        principal_id,
                        "--scope",
                        scope,
                        "--include-inherited",
                    ]
                )
            )
            for assignment in assignments:
                role_id = str(assignment.get("roleDefinitionId") or "").rsplit("/", 1)[-1]
                assignment_scope = str(assignment.get("scope") or scope).casefold()
                observed.add((assignment_scope, role_id.casefold()))
        return [
            (scope, role)
            for scope, role in required
            if not any(
                observed_role == role
                and scope.casefold().startswith(observed_scope)
                for observed_scope, observed_role in observed
            )
        ]

    def _owned_tags(self, value: Any) -> bool:
        return (
            isinstance(value, Mapping)
            and value.get("purpose") == _PURPOSE_TAG
            and str(value.get("agentInsightsQualityQualification") or "").casefold()
            == _QUALIFICATION_TAG
            and value.get("automationOwner") == self._owner
        )

    def _validate_cleanup_parent(self, item: Mapping[str, Any]) -> None:
        project_id = str(item.get("id") or "")
        try:
            account_id = project_id.rsplit("/projects/", 1)[0]
            group = project_id.strip("/").split("/")[
                project_id.strip("/").split("/").index("resourceGroups") + 1
            ]
            account = project_id.strip("/").split("/")[
                project_id.strip("/").split("/").index("accounts") + 1
            ]
        except (ValueError, IndexError) as error:
            raise RuntimeFailure("invalid_project_resource", "Project resource ID shape was invalid.") from error
        if self._config.resource_group and group != self._config.resource_group:
            raise RuntimeFailure("ownership_mismatch", "Cleanup project resource group did not match.")
        if self._config.account_name and account != self._config.account_name:
            raise RuntimeFailure("ownership_mismatch", "Cleanup project account did not match.")
        parent = _mapping(
            self._cli.json(["resource", "show", "--ids", account_id]),
            "invalid_foundry_account",
            "Cleanup project parent response was invalid.",
        )
        if (
            str(parent.get("type") or "").casefold() != "microsoft.cognitiveservices/accounts"
            or str(parent.get("kind") or "").casefold() != "aiservices"
            or not self._owned_tags(parent.get("tags"))
        ):
            raise RuntimeFailure("ownership_mismatch", "Cleanup project parent is not exactly owned.")

    def _connections(self, project_id: str) -> list[Mapping[str, Any]]:
        results: list[Mapping[str, Any]] = []
        next_url = f"{project_id}/connections?api-version=2025-06-01"
        while next_url:
            parsed = urllib.parse.urlparse(next_url)
            if parsed.netloc and parsed.netloc.casefold() != "management.azure.com":
                raise RuntimeFailure(
                    "invalid_azure_pagination",
                    "Azure connection pagination changed endpoint origin.",
                )
            if project_id.casefold() not in parsed.path.casefold():
                raise RuntimeFailure(
                    "invalid_azure_pagination",
                    "Azure connection pagination escaped the exact project.",
                )
            payload = self._cli.rest("get", next_url)
            results.extend(_items(payload))
            next_url = (
                str(payload.get("nextLink") or "")
                if isinstance(payload, Mapping)
                else ""
            )
        return results

    def _application_insights_connection(self, project_id: str) -> str:
        targets: list[str] = []
        for connection in self._connections(project_id):
            properties = connection.get("properties")
            if not isinstance(properties, Mapping):
                continue
            category = str(
                properties.get("category")
                or properties.get("connectionType")
                or ""
            ).casefold()
            if category not in {"appinsights", "applicationinsights"}:
                continue
            target = str(
                properties.get("target")
                or properties.get("targetResourceId")
                or ""
            )
            if target:
                targets.append(target)
        if len(targets) != 1:
            raise RuntimeFailure(
                "invalid_project_connections",
                "Project must have exactly one Application Insights connection.",
            )
        target = targets[0]
        if (
            "/providers/microsoft.insights/components/" not in target.casefold()
            or "?" in target
            or "#" in target
        ):
            raise RuntimeFailure(
                "invalid_project_connections",
                "Application Insights connection target was not an exact component resource ID.",
            )
        if (
            self._config.application_insights_resource_id
            and target.casefold()
            != self._config.application_insights_resource_id.casefold()
        ):
            raise RuntimeFailure(
                "project_selection_mismatch",
                "Application Insights connection differs from configuration.",
            )
        return target

    def _validate_terra(self, account: str, group: str) -> None:
        for deployment in (
            self._config.terra_agent_deployment,
            self._config.terra_insights_deployment,
        ):
            value = _mapping(
                self._cli.json(
                    [
                        "cognitiveservices",
                        "account",
                        "deployment",
                        "show",
                        "--resource-group",
                        group,
                        "--name",
                        account,
                        "--deployment-name",
                        deployment,
                    ]
                ),
                "missing_terra_deployment",
                "A required Terra deployment was not found.",
            )
            properties = value.get("properties")
            model = properties.get("model") if isinstance(properties, Mapping) else None
            model_name = str(model.get("name") or "") if isinstance(model, Mapping) else ""
            model_format = str(model.get("format") or "") if isinstance(model, Mapping) else ""
            model_version = str(model.get("version") or "") if isinstance(model, Mapping) else ""
            sku = value.get("sku")
            capacity = sku.get("capacity") if isinstance(sku, Mapping) else None
            state = str(properties.get("provisioningState") or "") if isinstance(properties, Mapping) else ""
            if (
                model_name.casefold() != "gpt-5.6-terra"
                or model_format.casefold() != "openai"
                or model_version != self._config.terra_model_version
                or state.casefold() != "succeeded"
                or not isinstance(capacity, (int, float))
                or capacity <= 0
            ):
                raise RuntimeFailure(
                    "invalid_terra_deployment",
                    "A configured Terra deployment is unavailable or has no capacity.",
                )

    def select_or_create(
        self,
        report_date: date,
        catalog_hash: str,
        *,
        allow_fallback: bool = False,
    ) -> ProjectResources:
        if allow_fallback and self._config.fallback_project_name:
            return self.discover_qualified()
        if not self._config.resource_group or not self._config.account_name:
            return self.discover_qualified()
        base = f"aiq-{report_date:%Y%m%d}"
        for suffix in range(100):
            name = base if suffix == 0 else f"{base}-r{suffix:02d}"
            projects = self._projects()
            existing = [
                item
                for item in projects
                if item.get("name") == name and self._under_configured_parent(item)
            ]
            if len(existing) == 1 and self._owned(existing[0]):
                tags = existing[0].get("tags")
                if (
                    isinstance(tags, Mapping)
                    and tags.get("reportDate") == report_date.isoformat()
                    and tags.get("catalogVersion") == catalog_hash
                ):
                    project_id = str(existing[0].get("id") or "")
                    item = self._wait_ready_item(project_id)
                    self._verify_created_project(item, report_date, catalog_hash)
                    return self._reconcile_project(item, project_id)
            if existing:
                continue
            expires = report_date + timedelta(days=7)
            project_id = (
                f"/subscriptions/{self._context.subscription_id}/resourceGroups/{self._config.resource_group}"
                f"/providers/Microsoft.CognitiveServices/accounts/{self._config.account_name}/projects/{name}"
            )
            body = {
                "location": "westus2",
                "identity": {"type": "SystemAssigned"},
                "tags": {
                    "purpose": _PURPOSE_TAG,
                    "agentInsightsQualityQualification": _QUALIFICATION_TAG,
                    "reportDate": report_date.isoformat(),
                    "expiresOn": expires.isoformat(),
                    "automationOwner": self._owner,
                    "catalogVersion": catalog_hash,
                },
                "properties": {},
            }
            if not self._cli.put_if_absent(
                f"{project_id}?api-version=2025-06-01",
                body,
            ):
                continue
            item = self._wait_ready_item(project_id)
            self._verify_created_project(item, report_date, catalog_hash)
            return self._reconcile_project(item, project_id)
        raise RuntimeFailure("project_name_exhausted", "No date-stamped project name is available.")

    def _under_configured_parent(self, item: Mapping[str, Any]) -> bool:
        project_id = str(item.get("id") or "")
        expected = (
            f"/subscriptions/{self._context.subscription_id}/resourceGroups/{self._config.resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{self._config.account_name}/projects/"
        )
        return project_id.casefold().startswith(expected.casefold())

    def _verify_created_project(
        self,
        item: Mapping[str, Any],
        report_date: date,
        catalog_hash: str,
    ) -> None:
        tags = item.get("tags")
        if (
            not self._owned(item)
            or not isinstance(tags, Mapping)
            or tags.get("reportDate") != report_date.isoformat()
            or tags.get("catalogVersion") != catalog_hash
            or not self._under_configured_parent(item)
        ):
            raise RuntimeFailure(
                "project_create_race",
                "Post-create verification did not match exact ownership, parent, date, and catalog.",
            )

    def _reconcile_project(
        self,
        item: Mapping[str, Any],
        project_id: str,
    ) -> ProjectResources:
        tags = item.get("tags")
        if not isinstance(tags, Mapping):
            raise RuntimeFailure("ownership_mismatch", "Project ownership tags are unavailable.")
        self._ensure_application_insights_connection(project_id, tags)
        self._ensure_project_roles(item, project_id)
        return self._validate_project(item, managed=True)

    def _ensure_project_roles(self, item: Mapping[str, Any], project_id: str) -> None:
        identity = item.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        principal_id = str(identity.get("principalId") or "")
        target = self._config.application_insights_resource_id
        if not principal_id or not target:
            raise RuntimeFailure(
                "project_identity_unavailable",
                "Project identity or App Insights target is unavailable for role assignment.",
            )
        account_id = project_id.rsplit("/projects/", 1)[0]
        missing = self._missing_project_roles(
            principal_id=principal_id,
            account_id=account_id,
            application_insights_id=target,
        )
        for scope, role in missing:
            result = self._cli.run(
                [
                    "role",
                    "assignment",
                    "create",
                    "--assignee-object-id",
                    principal_id,
                    "--assignee-principal-type",
                    "ServicePrincipal",
                    "--role",
                    role,
                    "--scope",
                    scope,
                ],
                allow_failure=True,
            )
            if result.returncode:
                raise RuntimeFailure(
                    "role_assignment_permission_blocked",
                    "Project was created but required roles could not be assigned. "
                    "Grant roleAssignments/write on the exact App Insights component and Foundry account.",
                )

    def _ensure_application_insights_connection(
        self,
        project_id: str,
        project_tags: Mapping[str, str],
    ) -> None:
        target = self._config.application_insights_resource_id
        if not target:
            raise RuntimeFailure(
                "missing_project_connections",
                "Project creation requires an explicit Application Insights resource ID.",
            )
        connection_id = f"{project_id}/connections/application-insights"
        connections = self._connections(project_id)
        reserved = [
            connection
            for connection in connections
            if str(connection.get("id") or "").casefold() == connection_id.casefold()
            or str(connection.get("name") or "") == "application-insights"
        ]
        exact = [
            connection
            for connection in reserved
            if str(connection.get("id") or "").casefold() == connection_id.casefold()
            and str(connection.get("name") or "") == "application-insights"
        ]
        app_insights = []
        for connection in connections:
            properties = connection.get("properties")
            category = (
                str(properties.get("category") or properties.get("connectionType") or "").casefold()
                if isinstance(properties, Mapping)
                else ""
            )
            if category in {"appinsights", "applicationinsights"}:
                app_insights.append(connection)
        if reserved:
            if len(reserved) != 1 or len(exact) != 1 or len(app_insights) != 1:
                raise RuntimeFailure(
                    "project_connection_conflict",
                    "Existing project connections cannot be safely reconciled.",
                )
            properties = exact[0].get("properties")
            metadata = properties.get("metadata") if isinstance(properties, Mapping) else None
            if (
                not isinstance(properties, Mapping)
                or str(properties.get("category") or properties.get("connectionType") or "").casefold()
                not in {"appinsights", "applicationinsights"}
                or str(properties.get("target") or properties.get("targetResourceId") or "").casefold()
                != target.casefold()
                or str(properties.get("authType") or "").casefold() != "aad"
                or not isinstance(metadata, Mapping)
                or metadata.get("purpose") != _PURPOSE_TAG
                or metadata.get("owner_reference") != self._owner
            ):
                raise RuntimeFailure(
                    "project_connection_conflict",
                    "Existing Application Insights connection is not exactly owned by this project.",
                )
            if metadata.get("expires_on") == project_tags["expiresOn"]:
                return
        if app_insights and not exact:
            raise RuntimeFailure(
                "project_connection_conflict",
                "A differently named Application Insights connection already exists.",
            )
        self._cli.rest(
            "put",
            f"{connection_id}?api-version=2025-06-01",
            {
                "properties": {
                    "category": "AppInsights",
                    "target": target,
                    "authType": "AAD",
                    "metadata": {
                        "purpose": _PURPOSE_TAG,
                        "owner_reference": self._owner,
                        "expires_on": project_tags["expiresOn"],
                    },
                }
            },
        )

    def _wait_ready_item(
        self,
        project_id: str,
        *,
        timeout_seconds: float = 900,
        poll_seconds: float = 10,
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            item = _mapping(
                self._cli.json(["resource", "show", "--ids", project_id]),
                "invalid_project_resource",
                "Project response was invalid.",
            )
            properties = item.get("properties")
            state = str(properties.get("provisioningState") or "") if isinstance(properties, Mapping) else ""
            if state.casefold() == "succeeded":
                if not self._owned(item):
                    raise RuntimeFailure("ownership_mismatch", "Project ownership tags do not match this runtime.")
                return item
            if state.casefold() in {"failed", "canceled", "cancelled"}:
                raise RuntimeFailure("project_provisioning_failed", "Project provisioning failed.")
            if time.monotonic() >= deadline:
                raise RuntimeFailure(
                    "project_readiness_timeout",
                    "Project did not become ready before the deadline.",
                    transient=True,
                )
            time.sleep(poll_seconds)

    def wait_ready(
        self,
        project_id: str,
        *,
        timeout_seconds: float = 900,
        poll_seconds: float = 10,
    ) -> ProjectResources:
        item = self._wait_ready_item(
            project_id,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
        return self._validate_project(item, managed=True)

    def cleanup_expired(self, *, now: date | None = None, dry_run: bool = True) -> list[str]:
        today = now or datetime.now(UTC).date()
        selected: list[str] = []
        failures = 0
        for item in self._projects():
            tags = item.get("tags")
            name = str(item.get("name") or "")
            if (
                not self._owned(item)
                or not _PROJECT_NAME.fullmatch(name)
                or name == self._config.fallback_project_name
                or not isinstance(tags, Mapping)
            ):
                continue
            try:
                expired = date.fromisoformat(str(tags.get("expiresOn") or "")) < today
            except ValueError:
                continue
            if not expired:
                continue
            try:
                self._validate_cleanup_parent(item)
            except RuntimeFailure:
                continue
            project_id = str(item.get("id") or "")
            if not dry_run:
                try:
                    self._cli.rest("delete", f"{project_id}?api-version=2025-06-01")
                except RuntimeFailure:
                    failures += 1
                    continue
            selected.append(name)
        if failures:
            raise RuntimeFailure(
                "cleanup_partial_failure",
                "One or more owned projects could not be deleted; other eligible projects were processed.",
                {"deleted_count": len(selected), "failure_count": failures},
            )
        return selected

    def cleanup_owned_connections(
        self,
        owner_reference: str,
        *,
        dry_run: bool = True,
    ) -> list[str]:
        selected: list[str] = []
        failures = 0
        today = datetime.now(UTC).date()
        for project in self._projects():
            if not self._owned(project):
                continue
            if project.get("name") == self._config.fallback_project_name:
                continue
            try:
                self._validate_cleanup_parent(project)
            except RuntimeFailure:
                continue
            project_id = str(project.get("id") or "")
            try:
                connections = self._connections(project_id)
            except RuntimeFailure:
                failures += 1
                continue
            for connection in connections:
                properties = connection.get("properties")
                metadata = properties.get("metadata") if isinstance(properties, Mapping) else None
                if (
                    not isinstance(metadata, Mapping)
                    or metadata.get("purpose") != _PURPOSE_TAG
                    or metadata.get("owner_reference") != owner_reference
                ):
                    continue
                try:
                    expired = date.fromisoformat(str(metadata.get("expires_on") or "")) < today
                except ValueError:
                    continue
                if not expired:
                    continue
                connection_id = str(connection.get("id") or "")
                if not connection_id.startswith(project_id + "/connections/"):
                    continue
                if not dry_run:
                    try:
                        self._cli.rest("delete", f"{connection_id}?api-version=2025-06-01")
                    except RuntimeFailure:
                        failures += 1
                        continue
                selected.append(str(connection.get("name") or ""))
        if failures:
            raise RuntimeFailure(
                "cleanup_partial_failure",
                "One or more owned project connections could not be processed; other eligible "
                "connections were processed.",
                {"processed_count": len(selected), "failure_count": failures},
            )
        return sorted(selected)
