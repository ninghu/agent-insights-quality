from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_cleanup import (
    CleanupEngine,
    CleanupPlanItem,
    build_cleanup_plan,
    cleanup_failure_summary,
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
        alert: Callable[[str], None],
        now: datetime | None = None,
    ) -> str:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        current = self._journal.read_active()
        if current.value["state"] in {"CLEAN", "FAILED_CLEAN"}:
            return str(current.value["state"])
        completed_successfully = bool(
            current.value["failure"] is None
            and current.value["evidence_reference"] is not None
        )
        if current.value["state"] != "CLEANING":
            current = self._journal.commit(
                current,
                next_state="CLEANING",
                now=moment,
            )
        resource_nonces = {
            item["ownership_nonce"] for item in current.value["resources"]
        }
        if len(resource_nonces) > 1:
            raise ContractError("Validation cleanup resources have mixed ownership")
        resource_nonce = (
            next(iter(resource_nonces))
            if resource_nonces
            else current.value["ownership_nonce"]
        )
        plan = build_cleanup_plan(
            cycle_id=current.value["cycle_id"],
            ownership_nonce=resource_nonce,
            resources=current.value["resources"],
            documented_project_cascade=self._policy.documented_project_cascade,
        )
        current = self._journal.commit(
            current,
            next_state="CLEANING",
            updates={
                "cleanup": {
                    "status": "in_progress",
                    "plan_hash": plan.plan_hash,
                    "failure": None,
                }
            },
            now=moment,
        )

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
                next_state="CLEANING",
                updates={"resources": resources},
                now=moment,
            )

        def record_discovery(item: CleanupPlanItem) -> None:
            nonlocal current
            if item.resolved_provider_id is None:
                raise ContractError("Resolved cleanup intent has no provider identity")
            resources = []
            found = False
            for resource in current.value["resources"]:
                updated = dict(resource)
                if resource["intent_reference"] == item.intent_reference:
                    updated["resolved_provider_id"] = item.resolved_provider_id
                    found = True
                resources.append(updated)
            if not found:
                raise ContractError("Resolved cleanup intent disappeared from journal")
            current = self._journal.commit(
                current,
                next_state="CLEANING",
                updates={"resources": resources},
                now=moment,
            )

        try:
            result = self._cleanup.execute(
                plan,
                record_delete_intent=record_intent,
                record_discovery=record_discovery,
            )
        except (ContractError, OSError, RuntimeError) as error:
            current = self._journal.commit(
                current,
                next_state="CLEANUP_BLOCKED",
                updates={
                    "cleanup": {
                        "status": "ambiguous",
                        "exact_clean": False,
                        "residue_ids": ["cleanup_unverified"],
                        "verification_at": moment.isoformat(),
                        "failure": cleanup_failure_summary(error),
                    }
                },
                now=moment,
            )
            try:
                alert("test_agent_validation_cleanup_blocked")
            except (OSError, RuntimeError):
                pass
            return str(current.value["state"])
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
            "failure": None,
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
        current = self._journal.commit(
            current,
            next_state=terminal,
            updates={
                "cleanup": cleanup,
                "resources": resources,
                "project": project,
            },
            now=moment,
        )
        if terminal == "CLEANUP_BLOCKED":
            try:
                alert("test_agent_validation_cleanup_blocked")
            except (OSError, RuntimeError):
                pass
        return str(current.value["state"])
