from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Callable

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.provisioning import FoundryProvisioner, RemoteHttpError
from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_cleanup import (
    CleanupInventory,
    CleanupPlanItem,
)

_ARM_API_VERSIONS = {
    "connection": "2025-06-01",
    "project": "2025-06-01",
    "role_assignment": "2022-04-01",
}
_HOSTED_TOPOLOGY_REFERENCES = {
    "hosted_identity": ("instance_identity", "client_id"),
    "hosted_blueprint": ("blueprint_reference", "blueprint_id"),
    "hosted_deployment": (None, "agent_guid"),
}
_HOSTED_RUNTIME_KINDS = {"hosted_code", "hosted_custom_container"}
_VERSION_DELETE_CONFLICT_DELAYS = (5, 10, 20, 30)


class AzureValidationCleanupBackend:
    def __init__(
        self,
        *,
        profile: RuntimeProfile,
        runtime_topology: Mapping[str, Any],
        resources: Sequence[Mapping[str, Any]],
        token_provider: Callable[[str], str],
        progress: ProgressReporter | None = None,
    ) -> None:
        self._profile = profile
        self._topology = {
            item["authority_id"]: item
            for item in runtime_topology.get("agents", [])
            if isinstance(item, Mapping)
        }
        self._resources = [dict(item) for item in resources]
        self._client = FoundryProvisioner(
            profile,
            token_provider=token_provider,
            progress=progress or ProgressReporter("aiq-validation-cleanup"),
        )

    def resolve_intent(self, item: CleanupPlanItem) -> CleanupPlanItem | None:
        parts = item.discovery_key.split("|")
        actual = ""
        deterministic_name = item.deterministic_name
        if item.kind in {
            "arm_deployment",
            "connection",
            "project",
            "role_assignment",
        }:
            actual = item.discovery_key
        elif item.kind == "provider_agent":
            actual = parts[0]
            deterministic_name = parts[0]
        elif item.kind == "provider_agent_version":
            version = self._find_version_by_logical(
                parts[0],
                parts[1],
                hosted=item.runtime_kind != "prompt",
            )
            if version:
                actual = f"{parts[0]}/versions/{version}"
                deterministic_name = f"{parts[0]}/{version}"
        elif item.kind in _HOSTED_TOPOLOGY_REFERENCES:
            version = self._find_version_by_logical(parts[0], parts[1], hosted=True)
            if version:
                try:
                    details = self._client.version_details(
                        parts[0],
                        version,
                        hosted=True,
                    )
                except RemoteHttpError as error:
                    if error.status != 404:
                        raise
                    details = {}
                if details:
                    field, child = _HOSTED_TOPOLOGY_REFERENCES[item.kind]
                    actual = _topology_reference(
                        details,
                        field,
                        child,
                    )
        elif item.kind == "session":
            actual = self._find_metadata_resource(
                f"/agents/{parts[0]}/endpoint/sessions?limit=100",
                item.intent_reference,
                hosted=True,
            )
        elif item.kind == "stored_response":
            if item.runtime_kind != "prompt":
                return (
                    replace(item, resolved_provider_id="discovery-absent")
                    if self._hosted_ephemeral_response_absent(item)
                    else None
                )
            if (
                len(parts) != 2
                or not parts[0]
                or parts[1] != item.intent_reference
            ):
                raise ContractError(
                    "Stored-response cleanup discovery key is invalid"
                )
            query = urllib.parse.urlencode(
                {"limit": 100, "agent_name": parts[0]}
            )
            actual = self._find_metadata_resource(
                f"/openai/v1/responses?{query}",
                item.intent_reference,
                hosted=False,
            )
        elif item.kind == "acr_tag":
            actual = item.discovery_key
        elif item.kind == "acr_manifest":
            actual = self._resolve_manifest_digest(item.discovery_key)
        elif item.kind == "runtime_principal":
            actual = item.intent_reference
        elif item.kind == "entra_service_principal":
            actual = self._find_service_principal(item.discovery_key)
        else:
            return None
        return replace(
            item,
            deterministic_name=deterministic_name,
            resolved_provider_id=actual or "discovery-absent",
        )

    def delete(self, item: CleanupPlanItem) -> None:
        if item.kind == "provider_agent_version":
            agent_name, version = self._provider_agent_version(item)
            for attempt in range(len(_VERSION_DELETE_CONFLICT_DELAYS) + 1):
                try:
                    self._client._delete_owned_version(
                        agent_name,
                        version,
                        hosted=item.runtime_kind != "prompt",
                    )
                    break
                except RemoteHttpError as error:
                    if (
                        attempt == len(_VERSION_DELETE_CONFLICT_DELAYS)
                        or error.status != 409
                        or error.code.casefold() != "conflict"
                    ):
                        raise
                    delay = _VERSION_DELETE_CONFLICT_DELAYS[attempt]
                    self._client.report_progress(
                        "Agent-version cleanup is waiting for session "
                        f"deletion propagation; retrying in {delay}s"
                    )
                    time.sleep(delay)
            return
        if item.kind == "provider_agent":
            self._client.delete_agent(
                item.deterministic_name,
                hosted=item.runtime_kind != "prompt",
            )
            return
        if item.kind == "session":
            self._client.delete_session(
                self._agent_name(item),
                self._actual_id(item),
            )
            return
        if item.kind == "stored_response":
            if item.runtime_kind != "prompt":
                try:
                    self._client.delete_session(
                        self._agent_name(item),
                        self._hosted_response_session_id(item),
                    )
                except RemoteHttpError as error:
                    if not _session_not_accessible(error):
                        raise
                return
            self._client.delete_response(self._actual_id(item))
            return
        if item.kind in _HOSTED_TOPOLOGY_REFERENCES:
            self._wait_hosted_topology_absent(item)
            return
        if item.kind == "entra_service_principal":
            self._run(
                [azure_cli(), "ad", "sp", "delete", "--id", self._actual_id(item)],
                expected=(0, 3),
            )
            return
        if item.kind == "acr_tag":
            repository, tag = _acr_tag(item.deterministic_name)
            self._run(
                [
                    azure_cli(),
                    "acr",
                    "repository",
                    "untag",
                    "--name",
                    self._profile.container_registry_name,
                    "--image",
                    f"{repository}:{tag}",
                ],
                expected=(0, 3),
            )
            return
        if item.kind == "acr_manifest":
            if self.manifest_is_shared(item.provider_id):
                return
            repository = item.deterministic_name
            self._run(
                [
                    azure_cli(),
                    "acr",
                    "repository",
                    "delete",
                    "--name",
                    self._profile.container_registry_name,
                    "--image",
                    f"{repository}@{self._actual_id(item)}",
                    "--yes",
                ],
                expected=(0, 3),
            )
            return
        if item.kind == "arm_deployment":
            self._cancel_wait_delete_deployment(item.deterministic_name)
            return
        actual_id = self._actual_id(item)
        if actual_id.startswith("/subscriptions/"):
            arguments = [
                azure_cli(),
                "resource",
                "delete",
                "--ids",
                actual_id,
            ]
            api_version = _ARM_API_VERSIONS.get(item.kind)
            if api_version is not None:
                arguments.extend(["--api-version", api_version])
            self._run(
                arguments,
                expected=(0, 3),
            )
            return
        raise ContractError(
            f"No explicit cleanup route for validation resource kind {item.kind}"
        )

    def _wait_hosted_topology_absent(self, item: CleanupPlanItem) -> None:
        deadline = time.monotonic() + 15 * 60
        next_progress = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.absent(item):
                return
            if time.monotonic() >= next_progress:
                self._client.report_progress(
                    f"{item.kind} cascade cleanup is still propagating"
                )
                next_progress = time.monotonic() + 60
            time.sleep(5)
        raise ContractError(
            f"{item.kind} cascade cleanup did not complete within 15 minutes"
        )

    def _hosted_ephemeral_response_absent(
        self,
        item: CleanupPlanItem,
    ) -> bool:
        try:
            return not self._client.session_exists(
                self._agent_name(item),
                self._hosted_response_session_id(item),
            )
        except RemoteHttpError as error:
            if _session_not_accessible(error):
                return True
            raise

    def _hosted_response_session_id(self, item: CleanupPlanItem) -> str:
        if item.runtime_kind not in _HOSTED_RUNTIME_KINDS or not item.parent_id:
            raise ContractError(
                "Hosted ephemeral response cleanup binding is invalid"
            )
        agent_name = self._agent_name(item)
        matches = [
            resource
            for resource in self._resources
            if resource.get("kind") == "session"
            and (
                resource.get("resolved_provider_id")
                or resource.get("provider_id")
            )
            == item.parent_id
        ]
        if len(matches) != 1:
            raise ContractError(
                "Hosted ephemeral response cleanup parent is ambiguous"
            )
        session = matches[0]
        session_agent = str(session.get("discovery_key") or "").split("|", 1)[0]
        if (
            session.get("authority_id") != item.authority_id
            or session.get("runtime_kind") != item.runtime_kind
            or session_agent != agent_name
        ):
            raise ContractError(
                "Hosted ephemeral response cleanup parent does not match"
            )
        return item.parent_id

    def absent(self, item: CleanupPlanItem) -> bool:
        if item.resolved_provider_id == "discovery-absent":
            return True
        if item.kind == "provider_agent_version":
            agent_name, version = self._provider_agent_version(item)
            return not self._client.version_exists(
                agent_name,
                version,
                hosted=item.runtime_kind != "prompt",
            )
        if item.kind == "provider_agent":
            return not self._client.agent_exists(
                item.deterministic_name,
                hosted=item.runtime_kind != "prompt",
            )
        if item.kind in _HOSTED_TOPOLOGY_REFERENCES:
            parts = item.discovery_key.split("|")
            if len(parts) != 3 or parts[2] != item.kind:
                raise ContractError(
                    "Hosted cleanup discovery key is invalid"
                )
            version = self._find_version_by_logical(
                parts[0],
                parts[1],
                hosted=True,
            )
            if not version:
                return True
            try:
                details = self._client.version_details(
                    parts[0],
                    version,
                    hosted=True,
                )
            except RemoteHttpError as error:
                if error.status == 404:
                    return True
                raise
            field, child = _HOSTED_TOPOLOGY_REFERENCES[item.kind]
            observed = _topology_reference(details, field, child)
            if observed != self._actual_id(item):
                raise ContractError("Hosted cleanup topology identity changed")
            return False
        if item.kind == "session":
            return not self._client.session_exists(
                self._agent_name(item),
                self._actual_id(item),
            )
        if item.kind == "stored_response":
            if item.runtime_kind != "prompt":
                return self._hosted_ephemeral_response_absent(item)
            return not self._client.response_exists(self._actual_id(item))
        if item.kind == "runtime_principal":
            actual_id = self._actual_id(item)
            if actual_id == item.intent_reference:
                if item.parent_id and item.parent_id.startswith("/subscriptions/"):
                    return self._arm_resource_absent(item.parent_id)
                return not self._client.agent_exists(
                    item.deterministic_name,
                    hosted=True,
                )
            return self._service_principal_absent(actual_id)
        if item.kind == "entra_service_principal":
            return self._service_principal_absent(self._actual_id(item))
        if item.kind == "acr_tag":
            repository, tag = _acr_tag(item.deterministic_name)
            process = self._run(
                [
                    azure_cli(),
                    "acr",
                    "repository",
                    "show-tags",
                    "--name",
                    self._profile.container_registry_name,
                    "--repository",
                    repository,
                    "--output",
                    "json",
                ],
                expected=(0, 3),
            )
            if process.returncode == 3:
                return True
            values = json.loads(process.stdout)
            return isinstance(values, list) and tag not in values
        if item.kind == "acr_manifest":
            actual_id = self._actual_id(item)
            return self.manifest_is_shared(actual_id) or self._manifest_absent(
                item.deterministic_name,
                actual_id,
            )
        if item.kind == "arm_deployment":
            return self._deployment_absent(item.deterministic_name)
        actual_id = self._actual_id(item)
        if actual_id.startswith("/subscriptions/"):
            return self._arm_resource_absent(
                actual_id,
                api_version=_ARM_API_VERSIONS.get(item.kind),
            )
        return False


    def manifest_is_shared(self, provider_id: str) -> bool:
        matches = [
            item
            for item in self._resources
            if item.get("kind") == "acr_manifest"
            and (
                item.get("resolved_provider_id") == provider_id
                or item.get("provider_id") == provider_id
                or str(item.get("discovery_key") or "").endswith(
                    f"@{provider_id}"
                )
            )
        ]
        if len(matches) != 1:
            return True
        repository = str(matches[0].get("deterministic_name") or "")
        process = self._run(
            [
                azure_cli(),
                "acr",
                "manifest",
                "show-metadata",
                "--registry",
                self._profile.container_registry_name,
                "--name",
                f"{repository}@{provider_id}",
                "--output",
                "json",
            ],
            expected=(0, 3),
        )
        if process.returncode == 3:
            return False
        value = json.loads(process.stdout)
        tags = value.get("tags") if isinstance(value, dict) else None
        current_tags = {
            str(item["deterministic_name"]).rsplit(":", 1)[-1]
            for item in self._resources
            if item.get("kind") == "acr_tag"
            and item.get("parent_id") == provider_id
        }
        return (
            not isinstance(tags, list)
            or not current_tags
            or any(not isinstance(tag, str) or tag not in current_tags for tag in tags)
        )

    def inventory(
        self,
        *,
        cycle_id: str,
        ownership_nonce: str,
    ) -> CleanupInventory:
        owned = self._run(
            [
                azure_cli(),
                "resource",
                "list",
                "--tag",
                f"ownershipNonce={ownership_nonce}",
                "--query",
                "[].id",
                "--output",
                "json",
            ],
            expected=(0,),
        )
        owned_values = json.loads(owned.stdout)
        nonce_owned = tuple(
            sorted(str(item) for item in owned_values)
        ) if isinstance(owned_values, list) else ()
        project = next(
            (
                item
                for item in self._resources
                if item.get("kind") == "project"
            ),
            None,
        )
        project_exists = bool(
            project
            and not self.absent(
                CleanupPlanItem(
                    kind="project",
                    deterministic_name=str(project["deterministic_name"]),
                    provider_id=str(project["provider_id"]),
                    resolved_provider_id=project.get("resolved_provider_id"),
                    intent_reference=str(project["intent_reference"]),
                    runtime_kind=str(project["runtime_kind"]),
                    discovery_key=str(project["discovery_key"]),
                    parent_id=None,
                    authority_id=None,
                    state=str(project["state"]),
                    cleanup_method="explicit",
                    shared_manifest_allowed=False,
                )
            )
        )
        sessions = tuple(
            sorted(
                item["provider_id"]
                for item in self._resources
                if item.get("kind") in {
                    "session",
                    "conversation",
                    "stored_response",
                }
                and not self.absent(_plan_item(item))
            )
        )
        tags = tuple(
            sorted(
                item["provider_id"]
                for item in self._resources
                if item.get("kind") == "acr_tag"
                and not self.absent(_plan_item(item))
            )
        )
        retained = tuple(
            sorted(
                item["provider_id"]
                for item in self._resources
                if item.get("kind") == "acr_manifest"
                and self.manifest_is_shared(str(item["provider_id"]))
            )
        )
        del cycle_id
        return CleanupInventory(
            project_exists=project_exists,
            nonce_owned_ids=nonce_owned,
            session_response_ids=sessions,
            cycle_acr_tag_ids=tags,
            incomplete_cascade_ids=(),
            retained_shared_manifest_ids=retained,
        )

    @staticmethod
    def _actual_id(item: CleanupPlanItem) -> str:
        return item.resolved_provider_id or item.provider_id

    @staticmethod
    def _agent_name(item: CleanupPlanItem) -> str:
        name = item.discovery_key.split("|", 1)[0]
        if not name:
            raise ContractError("Cleanup Agent discovery key is missing")
        return name

    @staticmethod
    def _provider_agent_version(
        item: CleanupPlanItem,
    ) -> tuple[str, str]:
        resolved = item.resolved_provider_id
        if resolved is None:
            return _agent_version(item.deterministic_name)
        marker = "/versions/"
        if marker not in resolved:
            raise ContractError(
                "Resolved validation Agent version identity is invalid"
            )
        agent_name, version = resolved.rsplit(marker, 1)
        discovery_agent = item.discovery_key.split("|", 1)[0]
        if (
            not agent_name
            or not version
            or agent_name != discovery_agent
        ):
            raise ContractError(
                "Resolved validation Agent version identity is inconsistent"
            )
        return agent_name, version

    def _find_version_by_logical(
        self,
        agent_name: str,
        logical_version: str,
        *,
        hosted: bool,
    ) -> str:
        response = self._client._request(
            "GET",
            f"/agents/{urllib.parse.quote(agent_name, safe='')}/versions?limit=100",
            hosted=hosted,
            expected={200, 404},
        )
        if response["_status"] == 404:
            return ""
        matches = [
            item
            for item in (response.get("data") or response.get("value") or [])
            if isinstance(item, Mapping)
            and isinstance(item.get("metadata"), Mapping)
            and item["metadata"].get("aiq_profile") == self._profile.name
            and item["metadata"].get("aiq_logical_version") == logical_version
        ]
        if len(matches) > 1:
            raise ContractError("Validation version discovery is ambiguous")
        return str(matches[0].get("version") or "") if matches else ""

    def _find_metadata_resource(
        self,
        route: str,
        intent_reference: str,
        *,
        hosted: bool,
    ) -> str:
        matches = []
        next_route = route
        seen_cursors: set[str] = set()
        while True:
            response = self._client._request(
                "GET",
                next_route,
                hosted=hosted,
                expected={200, 404},
            )
            if response["_status"] == 404:
                if not hosted:
                    raise ContractError(
                        "Stored-response intent discovery is unavailable"
                    )
                return ""
            matches.extend(
                item
                for item in (
                    response.get("data") or response.get("value") or []
                )
                if isinstance(item, Mapping)
                and isinstance(item.get("metadata"), Mapping)
                and item["metadata"].get("validation_intent_reference")
                == intent_reference
            )
            if len(matches) > 1:
                raise ContractError(
                    "Validation runtime resource discovery is ambiguous"
                )
            if response.get("has_more") is not True:
                break
            cursor = response.get("last_id")
            if (
                not isinstance(cursor, str)
                or not cursor
                or cursor in seen_cursors
            ):
                raise ContractError(
                    "Validation runtime resource pagination is invalid"
                )
            seen_cursors.add(cursor)
            separator = "&" if "?" in route else "?"
            next_route = (
                f"{route}{separator}"
                f"{urllib.parse.urlencode({'after': cursor})}"
            )
        if not matches:
            return ""
        provider_id = str(
            matches[0].get("agent_session_id")
            or matches[0].get("session_id")
            or matches[0].get("id")
            or ""
        )
        if not provider_id:
            raise ContractError(
                "Validation runtime resource discovery identity is missing"
            )
        return provider_id

    def _find_service_principal(self, discovery_key: str) -> str:
        process = self._run(
            [
                azure_cli(),
                "ad",
                "sp",
                "list",
                "--display-name",
                discovery_key,
                "--output",
                "json",
            ],
            expected=(0,),
        )
        values = json.loads(process.stdout)
        if not isinstance(values, list) or len(values) > 1:
            raise ContractError("Validation service-principal discovery is ambiguous")
        return str(values[0].get("id") or "") if values else ""

    def _resolve_manifest_digest(self, discovery_key: str) -> str:
        if "@" in discovery_key:
            return discovery_key.rsplit("@", 1)[-1]
        process = self._run(
            [
                azure_cli(),
                "acr",
                "manifest",
                "show-metadata",
                "--registry",
                self._profile.container_registry_name,
                "--name",
                discovery_key,
                "--output",
                "json",
            ],
            expected=(0, 3),
        )
        if process.returncode == 3:
            return ""
        value = json.loads(process.stdout)
        if not isinstance(value, Mapping):
            raise ContractError("Validation ACR manifest discovery is invalid")
        return str(value.get("digest") or value.get("changeableAttributes", {}).get("digest") or "")

    def _cancel_wait_delete_deployment(self, name: str) -> None:
        terminal = {"Succeeded", "Failed", "Canceled"}
        for attempt in range(31):
            state = self._run(
                [
                    azure_cli(),
                    "deployment",
                    "group",
                    "show",
                    "--resource-group",
                    "agent-insights-quality-rg",
                    "--name",
                    name,
                    "--query",
                    "properties.provisioningState",
                    "--output",
                    "tsv",
                ],
                expected=(0, 3),
            )
            if state.returncode == 3:
                return
            if state.stdout.strip() in terminal:
                break
            if attempt == 0:
                self._run(
                    [
                        azure_cli(),
                        "deployment",
                        "group",
                        "cancel",
                        "--resource-group",
                        "agent-insights-quality-rg",
                        "--name",
                        name,
                        "--output",
                        "none",
                    ],
                    expected=(0, 3),
                )
            if attempt < 30:
                time.sleep(5)
        else:
            raise ContractError("Validation deployment cleanup did not become terminal")
        self._run(
            [
                azure_cli(),
                "deployment",
                "group",
                "delete",
                "--resource-group",
                "agent-insights-quality-rg",
                "--name",
                name,
                "--output",
                "none",
            ],
            expected=(0, 3),
        )

    def _service_principal_absent(self, principal_id: str) -> bool:
        process = self._run(
            [
                azure_cli(),
                "ad",
                "sp",
                "show",
                "--id",
                principal_id,
                "--output",
                "none",
            ],
            expected=(0, 3),
        )
        return process.returncode == 3

    def _deployment_absent(self, name: str) -> bool:
        process = self._run(
            [
                azure_cli(),
                "deployment",
                "group",
                "show",
                "--resource-group",
                "agent-insights-quality-rg",
                "--name",
                name,
                "--output",
                "none",
            ],
            expected=(0, 3),
        )
        return process.returncode == 3

    def _arm_resource_absent(
        self,
        provider_id: str,
        *,
        api_version: str | None = None,
    ) -> bool:
        arguments = [
            azure_cli(),
            "resource",
            "show",
            "--ids",
            provider_id,
        ]
        if api_version is not None:
            arguments.extend(["--api-version", api_version])
        arguments.extend(["--output", "none"])
        process = self._run(
            arguments,
            expected=(0, 3),
        )
        return process.returncode == 3

    def _manifest_absent(self, repository: str, digest: str) -> bool:
        process = self._run(
            [
                azure_cli(),
                "acr",
                "manifest",
                "show-metadata",
                "--registry",
                self._profile.container_registry_name,
                "--name",
                f"{repository}@{digest}",
                "--output",
                "none",
            ],
            expected=(0, 3),
        )
        return process.returncode == 3

    @staticmethod
    def _run(
        arguments: list[str],
        *,
        expected: tuple[int, ...],
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if process.returncode not in expected:
            raise ContractError("Validation cleanup provider operation failed")
        return process


def _topology_reference(
    details: Mapping[str, Any],
    field: str | None,
    child: str,
) -> str:
    if field is None:
        nested = details
        path = child
    else:
        nested = details.get(field)
        path = f"{field}.{child}"
        if not isinstance(nested, Mapping):
            raise ContractError(
                f"Hosted cleanup topology field is invalid: {field}"
            )
    result = nested.get(child)
    if (
        not isinstance(result, str)
        or not result
        or result != result.strip()
    ):
        raise ContractError(
            f"Hosted cleanup topology field is invalid: {path}"
        )
    if field == "blueprint_reference" and (
        nested.get("type") != "ManagedAgentIdentityBlueprint"
    ):
        raise ContractError(
            "Hosted cleanup topology field is invalid: "
            "blueprint_reference.type"
        )
    return result


def _session_not_accessible(error: RemoteHttpError) -> bool:
    return (
        error.status == 403
        and error.code == "session_not_accessible"
    )


def _agent_version(value: str) -> tuple[str, str]:
    parts = value.rsplit("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ContractError("Validation Agent version cleanup name is invalid")
    return parts[0], parts[1]


def _acr_tag(value: str) -> tuple[str, str]:
    parts = value.rsplit(":", 1)
    if len(parts) != 2 or not all(parts):
        raise ContractError("Validation ACR tag cleanup name is invalid")
    return parts[0], parts[1]


def _plan_item(resource: Mapping[str, Any]) -> CleanupPlanItem:
    return CleanupPlanItem(
        kind=str(resource["kind"]),
        deterministic_name=str(resource["deterministic_name"]),
        provider_id=str(resource["provider_id"]),
        resolved_provider_id=resource.get("resolved_provider_id"),
        intent_reference=str(resource["intent_reference"]),
        runtime_kind=str(resource["runtime_kind"]),
        discovery_key=str(resource["discovery_key"]),
        parent_id=resource.get("parent_id"),
        authority_id=resource.get("authority_id"),
        state=str(resource["state"]),
        cleanup_method=str(resource["cleanup_method"]),
        shared_manifest_allowed=resource["kind"] == "acr_manifest",
    )
