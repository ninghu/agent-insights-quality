from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_insights_quality.util import (
    ContractError,
    atomic_json,
    content_hash,
    read_json,
)
from agent_insights_quality.validation_lifecycle import (
    LocalValidationLock,
    validation_runtime_root,
)
from agent_insights_quality.validation_cycle import ValidationCycleController
from agent_insights_quality.validation_runtime import AuthoritySpec

SHARD_COUNT = 10


def validate_shard_assignment(
    shard_id: int,
    authority_ids: Sequence[str],
    authorities: Sequence[AuthoritySpec],
) -> list[AuthoritySpec]:
    if (
        isinstance(shard_id, bool)
        or shard_id < 1
        or shard_id > SHARD_COUNT
        or not authority_ids
        or len(authority_ids) != len(set(authority_ids))
    ):
        raise ContractError("Validation shard assignment is invalid")
    by_id = {item.authority_id: item for item in authorities}
    if not set(authority_ids).issubset(by_id):
        raise ContractError("Validation shard contains an unknown authority")
    return [by_id[authority_id] for authority_id in authority_ids]


def shard_root(
    *,
    repository: str,
    pr_number: int,
    cycle_id: str,
    shard_id: int,
) -> Path:
    owner, name = repository.split("/", 1)
    return (
        validation_runtime_root()
        / "shards"
        / owner
        / name
        / str(pr_number)
        / cycle_id
        / f"shard-{shard_id:02d}"
    )


def shard_lock(
    *,
    repository: str,
    pr_number: int,
    cycle_id: str,
    shard_id: int,
) -> LocalValidationLock:
    return LocalValidationLock(
        shard_root(
            repository=repository,
            pr_number=pr_number,
            cycle_id=cycle_id,
            shard_id=shard_id,
        )
        / "validation.lock"
    )


def shard_binding(
    prepared: Mapping[str, Any],
    authority_ids: Sequence[str],
) -> dict[str, Any]:
    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    required_runtime_ids = set(authority_ids)
    canonical_agents = {
        runtime_by_id[authority_id]["canonical_agent"]
        for authority_id in authority_ids
    }
    required_runtime_ids.update(
        f"{agent}/v0" for agent in canonical_agents
    )
    if not required_runtime_ids.issubset(runtime_by_id):
        raise ContractError("Prepared topology lacks a shard authority binding")
    return {
        "repository": prepared["repository"],
        "pr_number": prepared["pr_number"],
        "commit_sha": prepared["commit_sha"],
        "cycle_id": prepared["cycle_id"],
        "validation_digest": prepared["digests"]["validation_digest"],
        "execution_matrix_digest": prepared["digests"][
            "execution_matrix_digest"
        ],
        "runtime_topology_digest": prepared["digests"][
            "runtime_topology_digest"
        ],
        "project_id": prepared["project"]["provider_id"],
        "authorities": [
            {
                "authority_id": authority_id,
                "runtime_agent_name": runtime_by_id[authority_id][
                    "runtime_agent_name"
                ],
                "runtime_agent_version": runtime_by_id[authority_id][
                    "runtime_agent_version"
                ],
                "provider_agent_id": runtime_by_id[authority_id][
                    "provider_agent_id"
                ],
                "provider_agent_version_id": runtime_by_id[authority_id][
                    "provider_agent_version_id"
                ],
                "provider_content_digest": runtime_by_id[authority_id][
                    "provider_content_digest"
                ],
            }
            for authority_id in sorted(required_runtime_ids)
        ],
    }


class ValidationShardStore:
    def __init__(
        self,
        *,
        prepared: Mapping[str, Any],
        shard_id: int,
        authority_ids: Sequence[str],
    ) -> None:
        self.shard_id = shard_id
        self.authority_ids = tuple(authority_ids)
        self.binding = shard_binding(prepared, authority_ids)
        self.root = shard_root(
            repository=str(prepared["repository"]),
            pr_number=int(prepared["pr_number"]),
            cycle_id=str(prepared["cycle_id"]),
            shard_id=shard_id,
        )
        self.invocation_path = self.root / "invocations.json"
        self.package_path = self.root / "package.json"
        self.cleanup_path = self.root / "cleanup.json"

    def begin_invocation(self) -> dict[str, Any]:
        if self.invocation_path.is_file():
            existing = self.read_invocations()
            has_ledger = bool(
                existing.get("resources") or existing.get("invocations")
            )
            if has_ledger:
                if not self.cleanup_path.is_file():
                    raise ContractError(
                        "Validation shard invocation already has an active ledger"
                    )
                cleanup = self.read_cleanup()
                if (
                    cleanup.get("status") != "clean"
                    or cleanup.get("exact_clean") is not True
                    or cleanup.get("safe_to_reinvoke") is not True
                    or cleanup.get("invocation_digest")
                    != existing["artifact_digest"]
                ):
                    raise ContractError(
                        "Validation shard invocation cleanup is not reusable"
                    )
        value = self._base("test-agent-validation-shard-invocations")
        value.update(
            {
                "status": "invoking",
                "resources": [],
                "invocations": [],
            }
        )
        return self._write(self.invocation_path, value)

    def record_resource(self, event: Mapping[str, Any]) -> None:
        value = self.read_invocations()
        resources = copy.deepcopy(value["resources"])
        resources.append(copy.deepcopy(dict(event)))
        value["resources"] = resources
        self._write(self.invocation_path, value)

    def record_authority(self, result: Mapping[str, Any]) -> None:
        value = self.read_invocations()
        invocations = [
            item
            for item in value["invocations"]
            if item["authority_id"] != result["authority_id"]
        ]
        invocations.append(copy.deepcopy(dict(result)))
        value["invocations"] = sorted(
            invocations,
            key=lambda item: item["authority_id"],
        )
        self._write(self.invocation_path, value)

    def complete_invocation(self) -> dict[str, Any]:
        value = self.read_invocations()
        if {
            item["authority_id"] for item in value["invocations"]
        } != set(self.authority_ids):
            raise ContractError("Validation shard invocation coverage is incomplete")
        value["status"] = "invoked"
        return self._write(self.invocation_path, value)

    def write_package(
        self,
        *,
        authorities: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        invocation = self.read_invocations()
        if invocation["status"] != "invoked":
            raise ContractError("Validation shard has not completed invocation")
        value = self._base("test-agent-validation-shard-package")
        value.update(
            {
                "invocation_digest": invocation["artifact_digest"],
                "authorities": copy.deepcopy(list(authorities)),
            }
        )
        return self._write(self.package_path, value)

    def read_invocations(self) -> dict[str, Any]:
        return self._read(
            self.invocation_path,
            "test-agent-validation-shard-invocations",
        )

    def read_package(self) -> dict[str, Any]:
        return self._read(
            self.package_path,
            "test-agent-validation-shard-package",
        )

    def read_cleanup(self) -> dict[str, Any]:
        return self._read(
            self.cleanup_path,
            "test-agent-validation-shard-cleanup",
        )

    def write_cleanup(
        self,
        *,
        invocation_digest: str,
        retained_count: int,
    ) -> dict[str, Any]:
        value = self._base("test-agent-validation-shard-cleanup")
        value.update(
            {
                "status": "clean",
                "exact_clean": True,
                "safe_to_reinvoke": True,
                "invocation_digest": invocation_digest,
                "retained_durable_count": retained_count,
            }
        )
        return self._write(self.cleanup_path, value)

    def _base(self, kind: str) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "kind": kind,
            "shard_id": self.shard_id,
            "authority_ids": list(self.authority_ids),
            "binding": copy.deepcopy(self.binding),
            "artifact_digest": "",
        }

    def _read(self, path: Path, kind: str) -> dict[str, Any]:
        if not path.is_file():
            raise ContractError(f"Validation shard {kind} artifact is missing")
        value = read_json(path)
        self._validate(value, kind)
        return value

    def _write(self, path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(value))
        result["artifact_digest"] = ""
        result["artifact_digest"] = content_hash(
            {
                key: item
                for key, item in result.items()
                if key != "artifact_digest"
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, result)
        return result

    def _validate(self, value: Mapping[str, Any], kind: str) -> None:
        expected = self._base(kind)
        if (
            value.get("schema_version") != "1.0.0"
            or value.get("kind") != kind
            or value.get("shard_id") != self.shard_id
            or value.get("authority_ids") != list(self.authority_ids)
            or value.get("binding") != expected["binding"]
            or value.get("artifact_digest")
            != content_hash(
                {
                    key: item
                    for key, item in value.items()
                    if key != "artifact_digest"
                }
            )
        ):
            raise ContractError("Validation shard artifact binding is invalid")


def compose_shard_authorities(
    packages: Sequence[Mapping[str, Any]],
    authorities: Sequence[AuthoritySpec],
) -> list[dict[str, Any]]:
    if (
        len(packages) != SHARD_COUNT
        or {item.get("shard_id") for item in packages}
        != set(range(1, SHARD_COUNT + 1))
    ):
        raise ContractError("Validation composition requires exactly 10 shards")
    expected = {item.authority_id for item in authorities}
    assigned = [
        authority_id
        for package in packages
        for authority_id in package["authority_ids"]
    ]
    evidence = [
        copy.deepcopy(item)
        for package in packages
        for item in package["authorities"]
    ]
    if (
        len(assigned) != len(set(assigned))
        or set(assigned) != expected
        or [item["authority_id"] for item in evidence]
        != assigned
        or any(
            package["binding"] != packages[0]["binding"]
            and {
                key: package["binding"][key]
                for key in package["binding"]
                if key != "authorities"
            }
            != {
                key: packages[0]["binding"][key]
                for key in packages[0]["binding"]
                if key != "authorities"
            }
            for package in packages[1:]
        )
    ):
        raise ContractError("Validation shard composition bindings are inconsistent")
    by_id = {item["authority_id"]: item for item in evidence}
    if len(by_id) != len(evidence):
        raise ContractError("Validation shard evidence coverage collides")
    return [by_id[item.authority_id] for item in authorities]


def import_shard_resources(
    controller: ValidationCycleController,
    invocation_artifacts: Sequence[Mapping[str, Any]],
    *,
    now: Any,
) -> None:
    for artifact in sorted(
        invocation_artifacts,
        key=lambda item: int(item["shard_id"]),
    ):
        for event in artifact["resources"]:
            if not _resource_event_is_imported(controller, event):
                controller.dynamic_resource_event(event, now=now())


def _resource_event_is_imported(
    controller: ValidationCycleController,
    event: Mapping[str, Any],
) -> bool:
    intent_reference = str(event.get("intent_reference") or "")
    existing = next(
        (
            item
            for item in controller.active.value["resources"]
            if item["intent_reference"] == intent_reference
        ),
        None,
    )
    if existing is None:
        return False
    expected = {
        "kind": event.get("kind"),
        "authority_id": event.get("authority_id"),
        "parent_id": event.get("parent_id"),
    }
    if (
        existing["ownership_nonce"]
        != controller.active.value["ownership_nonce"]
        or any(existing[key] != value for key, value in expected.items())
    ):
        raise ContractError("Validation shard resource import binding changed")
    state = event.get("state")
    if state == "create_intent":
        if (
            existing["runtime_kind"] != event.get("runtime_kind")
            or existing["discovery_key"] != event.get("discovery_key")
            or existing["cleanup_method"]
            != str(event.get("cleanup_method") or "explicit")
        ):
            raise ContractError("Validation shard resource intent changed")
        return True
    if state == "created":
        provider_id = str(event.get("provider_id") or "")
        collision = next(
            (
                item
                for item in controller.active.value["resources"]
                if item["provider_id"] == provider_id
                and item["intent_reference"] != intent_reference
            ),
            None,
        )
        if collision is not None:
            raise ContractError("Validation shard resource provider binding changed")
        if (
            existing["state"] in {"create_intent", "ambiguous_create"}
            and existing["provider_id"] == intent_reference
        ):
            return False
        if existing["provider_id"] != provider_id:
            raise ContractError("Validation shard resource provider binding changed")
        return existing["state"] in {
            "created",
            "delete_intent",
            "absence_verified",
        }
    if state == "ambiguous_create":
        return existing["state"] in {
            "ambiguous_create",
            "created",
            "delete_intent",
            "absence_verified",
        }
    raise ContractError("Validation shard resource event state is invalid")


def materialize_shard_resources(
    artifact: Mapping[str, Any],
    *,
    ownership_nonce: str,
    now: datetime,
) -> list[dict[str, Any]]:
    resources: dict[str, dict[str, Any]] = {}
    for event in artifact["resources"]:
        intent = str(event.get("intent_reference") or "")
        state = event.get("state")
        if state == "create_intent":
            resources.setdefault(
                intent,
                {
                    "kind": str(event["kind"]),
                    "parent_id": event.get("parent_id"),
                    "authority_id": event.get("authority_id"),
                    "deterministic_name": str(event["deterministic_name"]),
                    "intent_reference": intent,
                    "provider_id": intent,
                    "resolved_provider_id": None,
                    "runtime_kind": str(event["runtime_kind"]),
                    "discovery_key": str(event["discovery_key"]),
                    "ownership_nonce": ownership_nonce,
                    "create_intent_at": now.isoformat(),
                    "create_observed_at": None,
                    "delete_intent_at": None,
                    "delete_observed_at": None,
                    "state": "create_intent",
                    "cleanup_method": str(
                        event.get("cleanup_method") or "explicit"
                    ),
                },
            )
        elif state == "created" and intent in resources:
            resources[intent]["provider_id"] = str(event["provider_id"])
            resources[intent]["deterministic_name"] = str(
                event["deterministic_name"]
            )
            resources[intent]["state"] = "created"
            resources[intent]["create_observed_at"] = now.isoformat()
        elif state == "ambiguous_create" and intent in resources:
            resources[intent]["state"] = "ambiguous_create"
        else:
            raise ContractError("Validation shard resource event sequence is invalid")
    return list(resources.values())
