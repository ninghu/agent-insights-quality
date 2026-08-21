from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import RuntimeFailure

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
_DISPLAY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()'-]{0,126}$")
_CONTAINER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
_PROJECT = re.compile(r"^aiq-[0-9]{8}(?:-r[0-9]{2})?$")


def _required(source: Mapping[str, str], name: str) -> str:
    value = source.get(name, "").strip()
    if not value:
        raise RuntimeFailure("missing_runtime_configuration", f"Protected variable {name} is required.")
    return value


def _name(value: str, field: str) -> str:
    if not _NAME.fullmatch(value):
        raise RuntimeFailure("invalid_runtime_configuration", f"{field} has an invalid format.")
    return value


def _https(value: str, field: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeFailure("invalid_runtime_configuration", f"{field} must be an HTTPS endpoint.")
    return value.rstrip("/")


def _resource_id(value: str, field: str, subscription_id: str) -> str:
    prefix = f"/subscriptions/{subscription_id}/"
    if not value.casefold().startswith(prefix.casefold()) or ".." in value or "?" in value or "#" in value:
        raise RuntimeFailure(
            "invalid_runtime_configuration",
            f"{field} must be an exact resource ID in the selected subscription.",
        )
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AzureRuntimeConfig:
    subscription_id: str | None
    subscription_name: str | None
    expected_tenant_id: str | None
    expected_user_object_id: str | None
    resource_group: str | None
    account_name: str | None
    project_name: str | None
    project_endpoint: str | None
    application_insights_resource_id: str | None
    terra_agent_deployment: str
    terra_insights_deployment: str
    terra_model_version: str
    fallback_project_name: str | None


@dataclass(frozen=True, slots=True, repr=False)
class AdoRuntimeConfig:
    organization_url: str
    project: str
    template_id: str
    owner_id: str


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactRuntimeConfig:
    backend: str
    location: str
    container: str | None


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeConfig:
    azure: AzureRuntimeConfig
    ado: AdoRuntimeConfig
    artifacts: ArtifactRuntimeConfig
    automation_owner: str
    monitor_ownership_receipt: str
    adapter: str | None

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> RuntimeConfig:
        env = os.environ if source is None else source
        subscription_id = env.get("AIQ_AZURE_SUBSCRIPTION_ID", "").strip() or None
        subscription_name = env.get("AIQ_AZURE_SUBSCRIPTION_NAME", "").strip() or None
        if bool(subscription_id) == bool(subscription_name):
            raise RuntimeFailure(
                "invalid_runtime_configuration",
                "Set exactly one of AIQ_AZURE_SUBSCRIPTION_ID or AIQ_AZURE_SUBSCRIPTION_NAME.",
            )
        if subscription_id is not None and not _GUID.fullmatch(subscription_id):
            raise RuntimeFailure(
                "invalid_runtime_configuration",
                "AIQ_AZURE_SUBSCRIPTION_ID must be a subscription GUID.",
            )
        if subscription_name is not None:
            if not _DISPLAY_NAME.fullmatch(subscription_name):
                raise RuntimeFailure(
                    "invalid_runtime_configuration",
                    "subscription display name has an invalid format.",
                )
        expected_tenant_id = env.get("AIQ_AZURE_TENANT_ID", "").strip() or None
        expected_user_object_id = env.get("AIQ_AZURE_USER_OBJECT_ID", "").strip() or None
        for value, label in (
            (expected_tenant_id, "AIQ_AZURE_TENANT_ID"),
            (expected_user_object_id, "AIQ_AZURE_USER_OBJECT_ID"),
        ):
            if value is not None and not _GUID.fullmatch(value):
                raise RuntimeFailure(
                    "invalid_runtime_configuration",
                    f"{label} must be a GUID when supplied.",
                )
        resource_group = env.get("AIQ_AZURE_RESOURCE_GROUP", "").strip() or None
        account_name = env.get("AIQ_FOUNDRY_ACCOUNT", "").strip() or None
        project_name = env.get("AIQ_FOUNDRY_PROJECT", "").strip() or None
        project_endpoint = env.get("AIQ_FOUNDRY_PROJECT_ENDPOINT", "").strip() or None
        app_insights = env.get("AIQ_APPLICATION_INSIGHTS_RESOURCE_ID", "").strip() or None
        explicit_values = (resource_group, account_name, project_name, project_endpoint, app_insights)
        if any(explicit_values) and not all(explicit_values):
            raise RuntimeFailure(
                "invalid_runtime_configuration",
                "Explicit Azure coordinates must include resource group, account, project endpoint, "
                "and Application Insights resource ID.",
            )
        if resource_group is not None:
            _name(resource_group, "resource group")
            _name(account_name or "", "Foundry account")
            _name(project_name or "", "Foundry project")
            _https(project_endpoint or "", "project endpoint")
            if subscription_id is not None:
                _resource_id(app_insights or "", "Application Insights resource", subscription_id)
        fallback = env.get("AIQ_FALLBACK_PROJECT_NAME", "").strip() or None
        if fallback is not None:
            _name(fallback, "fallback project name")
        backend = _required(env, "AIQ_ARTIFACT_BACKEND").casefold()
        if backend not in {"local", "azure_blob"}:
            raise RuntimeFailure(
                "invalid_runtime_configuration",
                "AIQ_ARTIFACT_BACKEND must be local or azure_blob.",
            )
        container = env.get("AIQ_ARTIFACT_CONTAINER", "").strip() or None
        if backend == "azure_blob" and container is None:
            raise RuntimeFailure(
                "missing_runtime_configuration",
                "Protected variable AIQ_ARTIFACT_CONTAINER is required for azure_blob.",
            )
        artifact_location = _required(env, "AIQ_ARTIFACT_LOCATION")
        if backend == "azure_blob":
            _https(artifact_location, "artifact account")
            if container is None or not _CONTAINER.fullmatch(container):
                raise RuntimeFailure(
                    "invalid_runtime_configuration",
                    "AIQ_ARTIFACT_CONTAINER must be a valid private Blob container name.",
                )
        owner = _name(_required(env, "AIQ_AUTOMATION_OWNER"), "automation owner")
        if owner != "ninghu":
            raise RuntimeFailure(
                "invalid_runtime_configuration",
                "AIQ_AUTOMATION_OWNER must match the reviewed repository automation owner.",
            )
        return cls(
            azure=AzureRuntimeConfig(
                subscription_id=subscription_id,
                subscription_name=subscription_name,
                expected_tenant_id=expected_tenant_id,
                expected_user_object_id=expected_user_object_id,
                resource_group=resource_group,
                account_name=account_name,
                project_name=project_name,
                project_endpoint=project_endpoint.rstrip("/") if project_endpoint else None,
                application_insights_resource_id=app_insights,
                terra_agent_deployment=_name(
                    _required(env, "AIQ_TERRA_AGENT_DEPLOYMENT"), "Terra agent deployment"
                ),
                terra_insights_deployment=_name(
                    _required(env, "AIQ_TERRA_INSIGHTS_DEPLOYMENT"),
                    "Terra insights deployment",
                ),
                terra_model_version=_name(
                    _required(env, "AIQ_TERRA_MODEL_VERSION"),
                    "Terra model version",
                ),
                fallback_project_name=fallback,
            ),
            ado=AdoRuntimeConfig(
                organization_url=_https(_required(env, "AIQ_ADO_ORGANIZATION_URL"), "ADO organization"),
                project=_name(_required(env, "AIQ_ADO_PROJECT"), "ADO project"),
                template_id=_name(_required(env, "AIQ_ADO_TEMPLATE_ID"), "ADO template"),
                owner_id=_name(_required(env, "AIQ_ADO_OWNER_ID"), "ADO owner"),
            ),
            artifacts=ArtifactRuntimeConfig(
                backend=backend,
                location=artifact_location,
                container=container,
            ),
            automation_owner=owner,
            monitor_ownership_receipt=_required(env, "AIQ_MONITOR_OWNERSHIP_RECEIPT"),
            adapter=env.get("AIQ_RUNTIME_ADAPTER", "").strip() or None,
        )

    def public_summary(self) -> dict[str, str]:
        values = {
            "subscription": self.azure.subscription_id or self.azure.subscription_name or "",
            "resource_group": self.azure.resource_group or "discovered",
            "account": self.azure.account_name or "discovered",
            "project": self.azure.project_name or "discovered",
            "project_endpoint": self.azure.project_endpoint or "discovered",
            "application_insights": self.azure.application_insights_resource_id or "discovered",
            "tenant": self.azure.expected_tenant_id or "validated-current-user",
            "user": self.azure.expected_user_object_id or "validated-current-user",
            "ado": f"{self.ado.organization_url}/{self.ado.project}",
            "artifact_location": self.artifacts.location,
            "monitor_ownership_receipt": self.monitor_ownership_receipt,
        }
        return {
            key: "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
            for key, value in values.items()
        } | {
            "artifact_backend": self.artifacts.backend,
            "terra_agent_deployment": "sha256:"
            + hashlib.sha256(self.azure.terra_agent_deployment.encode("utf-8")).hexdigest(),
            "terra_insights_deployment": "sha256:"
            + hashlib.sha256(self.azure.terra_insights_deployment.encode("utf-8")).hexdigest(),
            "terra_model_version": "sha256:"
            + hashlib.sha256(self.azure.terra_model_version.encode("utf-8")).hexdigest(),
        }
