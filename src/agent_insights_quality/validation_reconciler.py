from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_cleanup import (
    CleanupEngine,
    CleanupPlanItem,
    build_cleanup_plan,
)
from agent_insights_quality.validation_lifecycle import LifecycleJournal
from agent_insights_quality.validation_policy import ValidationPolicy


class ValidationReconciler:
    def __init__(
        self,
        *,
        journal: LifecycleJournal,
        cleanup: CleanupEngine,
        policy: ValidationPolicy,
    ) -> None:
        self._journal = journal
        self._cleanup = cleanup
        self._policy = policy

    def reconcile(
        self,
        *,
        ownership_nonce: str,
        holder_workflow_reference: str,
        holder_app_reference: str,
        holder_run_reference: str,
        alert: Callable[[str], None],
        now: datetime | None = None,
    ) -> str:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        lease_id, takeover = self._journal.takeover_for_cleanup(
            ownership_nonce=ownership_nonce,
            holder_workflow_reference=holder_workflow_reference,
            holder_app_reference=holder_app_reference,
            holder_run_reference=holder_run_reference,
            now=moment,
        )
        current = takeover.active
        resource_nonces = {
            item["ownership_nonce"] for item in current.value["resources"]
        }
        if len(resource_nonces) > 1:
            raise ContractError("Validation cleanup resources have mixed ownership")
        resource_nonce = (
            next(iter(resource_nonces))
            if resource_nonces
            else current.value["lease"]["ownership_nonce"]
        )
        plan = build_cleanup_plan(
            cycle_id=current.value["cycle_id"],
            ownership_nonce=resource_nonce,
            resources=current.value["resources"],
            documented_project_cascade=self._policy.documented_project_cascade,
        )
        current = self._journal.commit(
            current,
            lease_id=lease_id,
            next_state="CLEANING",
            updates={
                "cleanup": {
                    "status": "in_progress",
                    "plan_hash": plan.plan_hash,
                }
            },
            now=moment,
        ).active

        def record_intent(item: CleanupPlanItem) -> None:
            nonlocal current
            resources = []
            found = False
            for resource in current.value["resources"]:
                updated = dict(resource)
                if resource["provider_id"] == item.provider_id:
                    updated["state"] = "delete_intent"
                    updated["delete_intent_at"] = moment.isoformat()
                    found = True
                resources.append(updated)
            if not found:
                raise ContractError("Cleanup intent resource disappeared from journal")
            current = self._journal.commit(
                current,
                lease_id=lease_id,
                next_state="CLEANING",
                updates={"resources": resources},
                now=moment,
            ).active

        result = self._cleanup.execute(
            plan,
            record_delete_intent=record_intent,
        )
        cleanup = {
            "status": "exact_clean" if result.exact_clean else "ambiguous",
            "plan_hash": result.plan_hash,
            "exact_clean": result.exact_clean,
            "verified_absent_ids": list(result.verified_absent_ids),
            "retained_shared_manifest_ids": list(
                result.retained_shared_manifest_ids
            ),
            "residue_ids": list(result.residue_ids),
            "verification_at": moment.isoformat(),
        }
        terminal = "FAILED_CLEAN" if result.exact_clean else "CLEANUP_BLOCKED"
        committed = self._journal.commit(
            current,
            lease_id=lease_id,
            next_state=terminal,
            updates={"cleanup": cleanup},
            now=moment,
        )
        if not result.exact_clean:
            try:
                alert("test_agent_validation_cleanup_blocked")
            except (OSError, RuntimeError):
                # Progress and alerting must not replace the durable blocked state.
                pass
        return committed.active.value["state"]
