from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_insights_quality.util import ContractError, content_hash

_DELETE_ORDER = {
    "stored_response": 0,
    "conversation": 1,
    "session": 2,
    "provider_agent_version": 3,
    "provider_agent": 4,
    "hosted_deployment": 5,
    "hosted_blueprint": 6,
    "hosted_identity": 7,
    "connection": 8,
    "role_assignment": 9,
    "runtime_principal": 10,
    "entra_service_principal": 11,
    "acr_tag": 12,
    "acr_manifest": 13,
    "project": 14,
}
_DISCOVERABLE_AMBIGUOUS_KINDS = {
    "acr_tag",
    "connection",
    "project",
    "provider_agent",
    "role_assignment",
    "runtime_principal",
}


@dataclass(frozen=True)
class CleanupPlanItem:
    kind: str
    deterministic_name: str
    provider_id: str
    parent_id: str | None
    authority_id: str | None
    state: str
    cleanup_method: str
    shared_manifest_allowed: bool


@dataclass(frozen=True)
class CleanupPlan:
    cycle_id: str
    ownership_nonce: str
    items: tuple[CleanupPlanItem, ...]
    plan_hash: str


@dataclass(frozen=True)
class CleanupInventory:
    project_exists: bool
    nonce_owned_ids: tuple[str, ...]
    session_response_ids: tuple[str, ...]
    cycle_acr_tag_ids: tuple[str, ...]
    incomplete_cascade_ids: tuple[str, ...]
    retained_shared_manifest_ids: tuple[str, ...] = ()

    @property
    def residue_ids(self) -> tuple[str, ...]:
        residue = {
            *self.nonce_owned_ids,
            *self.session_response_ids,
            *self.cycle_acr_tag_ids,
            *self.incomplete_cascade_ids,
        }
        if self.project_exists:
            residue.add("project")
        return tuple(sorted(residue))


@dataclass(frozen=True)
class CleanupResult:
    plan_hash: str
    exact_clean: bool
    verified_absent_ids: tuple[str, ...]
    retained_shared_manifest_ids: tuple[str, ...]
    residue_ids: tuple[str, ...]


class CleanupBackend(Protocol):
    def delete(self, item: CleanupPlanItem) -> None: ...

    def absent(self, item: CleanupPlanItem) -> bool: ...

    def manifest_is_shared(self, provider_id: str) -> bool: ...

    def inventory(
        self,
        *,
        cycle_id: str,
        ownership_nonce: str,
    ) -> CleanupInventory: ...


def build_cleanup_plan(
    *,
    cycle_id: str,
    ownership_nonce: str,
    resources: Sequence[Mapping[str, Any]],
    documented_project_cascade: Sequence[str],
) -> CleanupPlan:
    if not cycle_id or not ownership_nonce:
        raise ContractError("Cleanup cycle identity is required")
    cascade_kinds = set(documented_project_cascade)
    items: list[CleanupPlanItem] = []
    provider_ids: set[str] = set()
    for resource in resources:
        kind = str(resource.get("kind") or "")
        provider_id = str(resource.get("provider_id") or "")
        cleanup_method = str(resource.get("cleanup_method") or "")
        state = str(resource.get("state") or "")
        deterministic_name = str(resource.get("deterministic_name") or "")
        if kind not in _DELETE_ORDER or not provider_id or not deterministic_name:
            raise ContractError("Cleanup resource kind or provider ID is invalid")
        if provider_id in provider_ids:
            raise ContractError("Cleanup resource provider IDs must be unique")
        provider_ids.add(provider_id)
        if cleanup_method == "documented_project_cascade":
            if kind not in cascade_kinds:
                raise ContractError(
                    f"Cleanup cascade is not reviewed for resource kind {kind}"
                )
        elif cleanup_method != "explicit":
            raise ContractError("Cleanup method is invalid")
        if state not in {
            "create_intent",
            "created",
            "ambiguous_create",
            "delete_intent",
            "deleted",
            "absence_verified",
            "cleanup_blocked",
        }:
            raise ContractError("Cleanup resource state is invalid")
        items.append(
            CleanupPlanItem(
                kind=kind,
                deterministic_name=deterministic_name,
                provider_id=provider_id,
                parent_id=resource.get("parent_id"),
                authority_id=resource.get("authority_id"),
                state=state,
                cleanup_method=cleanup_method,
                shared_manifest_allowed=kind == "acr_manifest",
            )
        )
    items.sort(
        key=lambda item: (
            _DELETE_ORDER[item.kind],
            item.provider_id,
        )
    )
    payload = {
        "schema_version": "1.0.0",
        "cycle_id": cycle_id,
        "ownership_nonce": ownership_nonce,
        "items": [
            {
                "kind": item.kind,
                "provider_id": item.provider_id,
                "deterministic_name": item.deterministic_name,
                "parent_id": item.parent_id,
                "authority_id": item.authority_id,
                "state": item.state,
                "cleanup_method": item.cleanup_method,
                "shared_manifest_allowed": item.shared_manifest_allowed,
            }
            for item in items
        ],
    }
    return CleanupPlan(
        cycle_id=cycle_id,
        ownership_nonce=ownership_nonce,
        items=tuple(items),
        plan_hash=content_hash(payload),
    )


class CleanupEngine:
    def __init__(self, backend: CleanupBackend) -> None:
        self._backend = backend

    def execute(
        self,
        plan: CleanupPlan,
        *,
        record_delete_intent: Callable[[CleanupPlanItem], None],
    ) -> CleanupResult:
        if not plan.items:
            inventory = self._backend.inventory(
                cycle_id=plan.cycle_id,
                ownership_nonce=plan.ownership_nonce,
            )
            return _result(plan, (), inventory)
        for item in plan.items:
            if item.cleanup_method == "documented_project_cascade":
                continue
            record_delete_intent(item)
            if (
                item.kind == "acr_manifest"
                and self._backend.manifest_is_shared(item.provider_id)
            ):
                continue
            self._backend.delete(item)

        verified_absent = tuple(
            sorted(
                item.provider_id
                for item in plan.items
                if (
                    item.kind != "acr_manifest"
                    or not self._backend.manifest_is_shared(item.provider_id)
                )
                and (
                    item.state != "ambiguous_create"
                    or item.kind in _DISCOVERABLE_AMBIGUOUS_KINDS
                )
                and self._backend.absent(item)
            )
        )
        inventory = self._backend.inventory(
            cycle_id=plan.cycle_id,
            ownership_nonce=plan.ownership_nonce,
        )
        return _result(plan, verified_absent, inventory)


def _result(
    plan: CleanupPlan,
    verified_absent: tuple[str, ...],
    inventory: CleanupInventory,
) -> CleanupResult:
    retained = set(inventory.retained_shared_manifest_ids)
    residue = tuple(
        sorted(
            {
                *inventory.residue_ids,
                *(
                    item.provider_id
                    for item in plan.items
                    if item.provider_id not in verified_absent
                    and item.provider_id not in retained
                ),
            }
        )
    )
    return CleanupResult(
        plan_hash=plan.plan_hash,
        exact_clean=not residue,
        verified_absent_ids=verified_absent,
        retained_shared_manifest_ids=tuple(sorted(retained)),
        residue_ids=residue,
    )
