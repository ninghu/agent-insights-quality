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
        before = self._journal.read_active()
        if before.value["state"] in {"RECEIPT_ISSUED", "FAILED_CLEAN"}:
            return self._journal.release(
                before,
                lease_id=before.value["lease"]["lease_id"],
                now=moment,
            ).value["state"]
        if before.value["state"] == "CLEAN":
            resumed = self._journal.resume_pending_receipt(now=moment)
            if resumed is not None:
                return resumed.value["state"]
            expires = datetime.fromisoformat(
                before.value["absolute_expires_at"].replace("Z", "+00:00")
            ).astimezone(UTC)
            if moment < expires:
                return "CLEAN"
            return self._journal.abandon_expired_clean(
                ownership_nonce=ownership_nonce,
                holder_workflow_reference=holder_workflow_reference,
                holder_app_reference=holder_app_reference,
                holder_run_reference=holder_run_reference,
                now=moment,
            ).value["state"]
        completed_successfully = bool(
            before.value["failure"] is None
            and before.value["evidence_reference"] is not None
            and before.value["git"]["final_head_sha"] is not None
            and before.value["git"]["final_tree_sha"] is not None
        )
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
        terminal = (
            "CLEAN"
            if result.exact_clean and completed_successfully
            else "FAILED_CLEAN"
            if result.exact_clean
            else "CLEANUP_BLOCKED"
        )
        resources = []
        absent = set(result.verified_absent_ids)
        for resource in current.value["resources"]:
            value = dict(resource)
            if value["provider_id"] in absent:
                value["state"] = "absence_verified"
                value["delete_observed_at"] = moment.isoformat()
            resources.append(value)
        project = dict(current.value["project"])
        if project["provider_id"] in absent:
            project["state"] = "deleted"
            project["delete_observed_at"] = moment.isoformat()
        committed = self._journal.commit(
            current,
            lease_id=lease_id,
            next_state=terminal,
            updates={
                "cleanup": cleanup,
                "resources": resources,
                "project": project,
            },
            now=moment,
        )
        if not result.exact_clean:
            try:
                alert("test_agent_validation_cleanup_blocked")
            except (OSError, RuntimeError):
                # Progress and alerting must not replace the durable blocked state.
                pass
        active = committed.active
        if terminal == "FAILED_CLEAN":
            active = self._journal.release(
                active,
                lease_id=lease_id,
                now=moment,
            )
        return active.value["state"]
