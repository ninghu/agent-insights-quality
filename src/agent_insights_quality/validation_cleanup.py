from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_insights_quality.util import ContractError, content_hash

_DELETE_ORDER = {
    "arm_deployment": 0,
    "stored_response": 1,
    "conversation": 2,
    "session": 3,
    "provider_agent_version": 4,
    "provider_agent": 5,
    "hosted_deployment": 6,
    "hosted_blueprint": 7,
    "hosted_identity": 8,
    "connection": 9,
    "role_assignment": 10,
    "runtime_principal": 11,
    "entra_service_principal": 12,
    "acr_tag": 13,
    "acr_manifest": 14,
    "project": 15,
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
    resolved_provider_id: str | None
    intent_reference: str
    runtime_kind: str
    discovery_key: str
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


class CleanupOperationError(ContractError):
    def __init__(self, operation: str, resource_kind: str) -> None:
        super().__init__(
            f"Validation cleanup {operation} failed for {resource_kind}"
        )
        self.operation = operation
        self.resource_kind = resource_kind


class CleanupBackend(Protocol):
    def resolve_intent(self, item: CleanupPlanItem) -> CleanupPlanItem | None: ...

    def delete(self, item: CleanupPlanItem) -> None: ...

    def absent(self, item: CleanupPlanItem) -> bool: ...

    def manifest_is_shared(self, provider_id: str) -> bool: ...

    def inventory(
        self,
        *,
        cycle_id: str,
        ownership_nonce: str,
    ) -> CleanupInventory: ...


def cleanup_failure_summary(error: BaseException) -> dict[str, Any]:
    operation = "cleanup_cycle"
    resource_kind = "unknown"
    cause = error
    if isinstance(error, CleanupOperationError):
        operation = error.operation
        resource_kind = error.resource_kind
        if error.__cause__ is not None:
            cause = error.__cause__
    status = getattr(cause, "status", None)
    http_status = (
        status
        if isinstance(status, int)
        and not isinstance(status, bool)
        and 100 <= status <= 599
        else None
    )
    provider_code = _normalized_provider_code(getattr(cause, "code", None))
    return {
        "operation": operation,
        "resource_kind": resource_kind,
        "http_status": http_status,
        "provider_code": provider_code,
        "error_class": type(cause).__name__,
    }


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
    intent_references: set[str] = set()
    for resource in resources:
        kind = str(resource.get("kind") or "")
        provider_id = str(resource.get("provider_id") or "")
        intent_reference = str(resource.get("intent_reference") or "")
        runtime_kind = str(resource.get("runtime_kind") or "")
        discovery_key = str(resource.get("discovery_key") or "")
        cleanup_method = str(resource.get("cleanup_method") or "")
        state = str(resource.get("state") or "")
        deterministic_name = str(resource.get("deterministic_name") or "")
        if (
            kind not in _DELETE_ORDER
            or not provider_id
            or not deterministic_name
            or not intent_reference
            or not runtime_kind
            or not discovery_key
        ):
            raise ContractError("Cleanup resource kind or provider ID is invalid")
        if provider_id in provider_ids:
            raise ContractError("Cleanup resource provider IDs must be unique")
        if intent_reference in intent_references:
            raise ContractError("Cleanup resource intent references must be unique")
        provider_ids.add(provider_id)
        intent_references.add(intent_reference)
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
                resolved_provider_id=resource.get("resolved_provider_id"),
                intent_reference=intent_reference,
                runtime_kind=runtime_kind,
                discovery_key=discovery_key,
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
                "resolved_provider_id": item.resolved_provider_id,
                "intent_reference": item.intent_reference,
                "runtime_kind": item.runtime_kind,
                "discovery_key": item.discovery_key,
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
        record_discovery: Callable[[CleanupPlanItem], None] = lambda _item: None,
    ) -> CleanupResult:
        if not plan.items:
            inventory = self._operation(
                "inventory",
                "inventory",
                lambda: self._backend.inventory(
                    cycle_id=plan.cycle_id,
                    ownership_nonce=plan.ownership_nonce,
                ),
            )
            return _result(plan, (), inventory)
        resolved_items: list[CleanupPlanItem] = []
        unresolved: list[CleanupPlanItem] = []
        for item in plan.items:
            if (
                item.state not in {"create_intent", "ambiguous_create"}
                or item.resolved_provider_id is not None
            ):
                resolved_items.append(item)
                continue
            resolved = self._operation(
                "resolve_intent",
                item.kind,
                lambda item=item: self._backend.resolve_intent(item),
            )
            if resolved is None:
                unresolved.append(item)
                continue
            record_discovery(resolved)
            resolved_items.append(resolved)
        for item in resolved_items:
            if item.cleanup_method == "documented_project_cascade":
                continue
            if self._absent(item):
                continue
            record_delete_intent(item)
            if (
                item.kind == "acr_manifest"
                and self._manifest_is_shared(item)
            ):
                continue
            self._operation(
                "delete",
                item.kind,
                lambda item=item: self._backend.delete(item),
            )

        verified_absent = tuple(
            sorted(
                item.provider_id
                for item in resolved_items
                if (
                    item.kind != "acr_manifest"
                    or not self._manifest_is_shared(item)
                )
                and (
                    item.state not in {"create_intent", "ambiguous_create"}
                    or item.resolved_provider_id is not None
                    or item.kind in _DISCOVERABLE_AMBIGUOUS_KINDS
                )
                and self._absent(item)
            )
        )
        inventory = self._operation(
            "inventory",
            "inventory",
            lambda: self._backend.inventory(
                cycle_id=plan.cycle_id,
                ownership_nonce=plan.ownership_nonce,
            ),
        )
        return _result(plan, verified_absent, inventory, unresolved=unresolved)

    def _absent(self, item: CleanupPlanItem) -> bool:
        return self._operation(
            "verify_absent",
            item.kind,
            lambda: self._backend.absent(item),
        )

    def _manifest_is_shared(self, item: CleanupPlanItem) -> bool:
        return self._operation(
            "verify_shared_manifest",
            item.kind,
            lambda: self._backend.manifest_is_shared(
                item.resolved_provider_id or item.provider_id
            ),
        )

    @staticmethod
    def _operation(
        operation: str,
        resource_kind: str,
        callback: Callable[[], Any],
    ) -> Any:
        try:
            return callback()
        except CleanupOperationError:
            raise
        except (
            ContractError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            raise CleanupOperationError(operation, resource_kind) from error


def _result(
    plan: CleanupPlan,
    verified_absent: tuple[str, ...],
    inventory: CleanupInventory,
    *,
    unresolved: Sequence[CleanupPlanItem] = (),
) -> CleanupResult:
    retained = set(inventory.retained_shared_manifest_ids)
    residue = tuple(
        sorted(
            {
                *inventory.residue_ids,
                *(item.provider_id for item in unresolved),
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


def _normalized_provider_code(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", value.strip())
    normalized = re.sub(r"[^a-z0-9]+", "_", snake.casefold()).strip("_")
    return normalized[:64] or None
