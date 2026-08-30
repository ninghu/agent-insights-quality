from __future__ import annotations

import pytest

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_cleanup import (
    CleanupEngine,
    CleanupInventory,
    CleanupPlanItem,
    build_cleanup_plan,
)


def _resource(
    kind: str,
    provider_id: str,
    *,
    cleanup_method: str = "explicit",
) -> dict:
    return {
        "kind": kind,
        "provider_id": provider_id,
        "deterministic_name": provider_id,
        "parent_id": "project-id" if kind != "project" else None,
        "authority_id": None,
        "cleanup_method": cleanup_method,
        "state": "created",
    }


class Backend:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.shared: set[str] = set()
        self.residue: tuple[str, ...] = ()

    def delete(self, item: CleanupPlanItem) -> None:
        self.deleted.append(item.provider_id)

    def absent(self, item: CleanupPlanItem) -> bool:
        return item.provider_id in self.deleted

    def manifest_is_shared(self, provider_id: str) -> bool:
        return provider_id in self.shared

    def inventory(
        self,
        *,
        cycle_id: str,
        ownership_nonce: str,
    ) -> CleanupInventory:
        assert cycle_id == "validation-cycle-0001"
        assert ownership_nonce == "nonce-0001"
        return CleanupInventory(
            project_exists=False,
            nonce_owned_ids=self.residue,
            session_response_ids=(),
            cycle_acr_tag_ids=(),
            incomplete_cascade_ids=(),
            retained_shared_manifest_ids=tuple(sorted(self.shared)),
        )


def test_cleanup_is_intent_first_reverse_dependency_and_exhaustive() -> None:
    plan = build_cleanup_plan(
        cycle_id="validation-cycle-0001",
        ownership_nonce="nonce-0001",
        resources=[
            _resource("project", "project-id"),
            _resource("provider_agent", "agent-id"),
            _resource("stored_response", "response-id"),
            _resource("conversation", "conversation-id"),
        ],
        documented_project_cascade=(),
    )
    backend = Backend()
    intents: list[str] = []
    result = CleanupEngine(backend).execute(
        plan,
        record_delete_intent=lambda item: intents.append(item.provider_id),
    )
    assert intents == backend.deleted == [
        "response-id",
        "conversation-id",
        "agent-id",
        "project-id",
    ]
    assert result.exact_clean is True
    assert result.residue_ids == ()


def test_cleanup_allows_only_reviewed_cascade_and_shared_acr_manifest() -> None:
    with pytest.raises(ContractError, match="cascade is not reviewed"):
        build_cleanup_plan(
            cycle_id="validation-cycle-0001",
            ownership_nonce="nonce-0001",
            resources=[
                _resource(
                    "provider_agent",
                    "agent-id",
                    cleanup_method="documented_project_cascade",
                )
            ],
            documented_project_cascade=(),
        )
    plan = build_cleanup_plan(
        cycle_id="validation-cycle-0001",
        ownership_nonce="nonce-0001",
        resources=[_resource("acr_manifest", "manifest-id")],
        documented_project_cascade=(),
    )
    backend = Backend()
    backend.shared.add("manifest-id")
    result = CleanupEngine(backend).execute(
        plan,
        record_delete_intent=lambda item: None,
    )
    assert backend.deleted == []
    assert result.exact_clean is True
    assert result.retained_shared_manifest_ids == ("manifest-id",)


def test_cleanup_residue_fails_closed() -> None:
    plan = build_cleanup_plan(
        cycle_id="validation-cycle-0001",
        ownership_nonce="nonce-0001",
        resources=[_resource("project", "project-id")],
        documented_project_cascade=(),
    )
    backend = Backend()
    backend.residue = ("nonce-owned-resource",)
    result = CleanupEngine(backend).execute(
        plan,
        record_delete_intent=lambda item: None,
    )
    assert result.exact_clean is False
    assert result.residue_ids == ("nonce-owned-resource",)


def test_ambiguous_create_cannot_be_proven_clean_from_placeholder_absence() -> None:
    resource = _resource("stored_response", "intent-digest")
    resource["state"] = "ambiguous_create"
    plan = build_cleanup_plan(
        cycle_id="validation-cycle-0001",
        ownership_nonce="nonce-0001",
        resources=[resource],
        documented_project_cascade=(),
    )
    backend = Backend()
    result = CleanupEngine(backend).execute(
        plan,
        record_delete_intent=lambda item: None,
    )
    assert result.exact_clean is False
    assert result.residue_ids == ("intent-digest",)
