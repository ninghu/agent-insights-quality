from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.provisioning import FoundryProvisioner
from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_cleanup import (
    CleanupInventory,
    CleanupPlanItem,
)


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

    def delete(self, item: CleanupPlanItem) -> None:
        if item.kind == "provider_agent_version":
            agent_name, version = _agent_version(item.deterministic_name)
            self._client._delete_owned_version(
                agent_name,
                version,
                hosted=self._hosted(item.authority_id),
            )
            return
        if item.kind == "provider_agent":
            self._client.delete_agent(
                item.deterministic_name,
                hosted=self._hosted(item.authority_id),
            )
            return
        if item.kind == "session":
            self._client.delete_session(
                self._agent_name(item.authority_id),
                item.provider_id,
            )
            return
        if item.kind == "stored_response":
            self._client.delete_response(item.provider_id)
            return
        if item.kind == "entra_service_principal":
            self._run(
                [azure_cli(), "ad", "sp", "delete", "--id", item.provider_id],
                expected=(0,),
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
                expected=(0,),
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
                    f"{repository}@{item.provider_id}",
                    "--yes",
                ],
                expected=(0,),
            )
            return
        if item.provider_id.startswith("/subscriptions/"):
            self._run(
                [
                    azure_cli(),
                    "resource",
                    "delete",
                    "--ids",
                    item.provider_id,
                ],
                expected=(0,),
            )
            return
        raise ContractError(
            f"No explicit cleanup route for validation resource kind {item.kind}"
        )

    def absent(self, item: CleanupPlanItem) -> bool:
        if item.kind == "provider_agent_version":
            agent_name, version = _agent_version(item.deterministic_name)
            return not self._client.version_exists(
                agent_name,
                version,
                hosted=self._hosted(item.authority_id),
            )
        if item.kind == "provider_agent":
            return not self._client.agent_exists(
                item.deterministic_name,
                hosted=self._hosted(item.authority_id),
            )
        if item.kind == "session":
            return not self._client.session_exists(
                self._agent_name(item.authority_id),
                item.provider_id,
            )
        if item.kind == "stored_response":
            return not self._client.response_exists(item.provider_id)
        if item.kind == "runtime_principal":
            return self._service_principal_absent(item.provider_id)
        if item.kind == "entra_service_principal":
            return self._service_principal_absent(item.provider_id)
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
            return self.manifest_is_shared(item.provider_id) or self._manifest_absent(
                item.deterministic_name,
                item.provider_id,
            )
        if item.provider_id.startswith("/subscriptions/"):
            process = self._run(
                [
                    azure_cli(),
                    "resource",
                    "show",
                    "--ids",
                    item.provider_id,
                    "--output",
                    "none",
                ],
                expected=(0, 3),
            )
            return process.returncode == 3
        return False

    def manifest_is_shared(self, provider_id: str) -> bool:
        matches = [
            item
            for item in self._resources
            if item.get("kind") == "acr_manifest"
            and item.get("provider_id") == provider_id
        ]
        if len(matches) != 1:
            return False
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
        return isinstance(tags, list) and any(
            isinstance(tag, str) and not tag.startswith("validation-")
            for tag in tags
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

    def _hosted(self, authority_id: str | None) -> bool:
        if authority_id is None or authority_id not in self._topology:
            raise ContractError("Cleanup authority topology is missing")
        return self._topology[authority_id]["runtime_kind"] != "prompt"

    def _agent_name(self, authority_id: str | None) -> str:
        if authority_id is None or authority_id not in self._topology:
            raise ContractError("Cleanup authority topology is missing")
        return str(self._topology[authority_id]["runtime_agent_name"])

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
        parent_id=resource.get("parent_id"),
        authority_id=resource.get("authority_id"),
        state=str(resource["state"]),
        cleanup_method=str(resource["cleanup_method"]),
        shared_manifest_allowed=resource["kind"] == "acr_manifest",
    )
