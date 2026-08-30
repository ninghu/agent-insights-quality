from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    content_hash,
    immutable_json,
    read_json,
)
from agent_insights_quality.validation_blob import BlobRecord
from agent_insights_quality.validation_policy import ValidationPolicy

LIFECYCLE_SCHEMA = (
    ROOT / "schemas" / "test-agent-validation-lifecycle.schema.json"
)
ACTIVE_CONTAINER = "test-agent-validation-lifecycle"
SNAPSHOT_CONTAINER = "test-agent-validation-snapshots"
ACTIVE_BLOB = "active.json"

STATES = (
    "LEASED",
    "PREFLIGHT",
    "CREATING",
    "VALIDATING",
    "FROZEN",
    "REVIEWED",
    "SHADOW_REVIEW_SKIPPED",
    "REVALIDATING",
    "FINAL_CHECKS",
    "CLEANING",
    "CLEAN",
    "RECEIPT_ISSUED",
    "FAILED_CLEAN",
    "CLEANUP_BLOCKED",
)
_TRANSITIONS = {
    "LEASED": {"PREFLIGHT", "CLEANING"},
    "PREFLIGHT": {"CREATING", "CLEANING", "FAILED_CLEAN"},
    "CREATING": {"VALIDATING", "CLEANING"},
    "VALIDATING": {"FROZEN", "CLEANING"},
    "FROZEN": {"REVIEWED", "SHADOW_REVIEW_SKIPPED", "CLEANING"},
    "REVIEWED": {"REVALIDATING", "CLEANING"},
    "SHADOW_REVIEW_SKIPPED": {"REVALIDATING", "CLEANING"},
    "REVALIDATING": {"FINAL_CHECKS", "CLEANING"},
    "FINAL_CHECKS": {"CLEANING"},
    "CLEANING": {"CLEAN", "FAILED_CLEAN", "CLEANUP_BLOCKED"},
    "CLEAN": {"RECEIPT_ISSUED", "FAILED_CLEAN"},
    "RECEIPT_ISSUED": set(),
    "FAILED_CLEAN": set(),
    "CLEANUP_BLOCKED": {"CLEANING"},
}
_POST_TTL_STATES = {
    "CLEANING",
    "CLEAN",
    "FAILED_CLEAN",
    "CLEANUP_BLOCKED",
    "RECEIPT_ISSUED",
}


class LifecycleStore(Protocol):
    def read_optional(
        self,
        container: str,
        name: str,
        *,
        lease_id: str | None = None,
    ) -> BlobRecord | None: ...

    def read(
        self,
        container: str,
        name: str,
        *,
        lease_id: str | None = None,
    ) -> BlobRecord: ...

    def create_once(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
    ) -> BlobRecord: ...

    def compare_and_swap(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
        *,
        lease_id: str,
        etag: str,
    ) -> BlobRecord: ...

    def acquire_infinite_lease(
        self,
        container: str,
        name: str,
        *,
        proposed_lease_id: str | None = None,
    ) -> str: ...

    def break_lease(self, container: str, name: str) -> None: ...

    def release_lease(
        self,
        container: str,
        name: str,
        *,
        lease_id: str,
    ) -> None: ...


@dataclass(frozen=True)
class LifecycleCommit:
    active: BlobRecord
    event: BlobRecord
    clean: BlobRecord | None


def lifecycle_digest(value: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop("journal_digest", None)
    return content_hash(payload)


def stamp_lifecycle_digest(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["journal_digest"] = lifecycle_digest(result)
    return result


def validate_lifecycle(value: Mapping[str, Any]) -> None:
    schema = read_json(LIFECYCLE_SCHEMA)
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"Test Agent validation lifecycle schema error at {location}: "
            f"{error.message}"
        )
    if value["journal_digest"] != lifecycle_digest(value):
        raise ContractError("Validation lifecycle journal digest is stale")
    if value["epoch"] != value["lease"]["epoch"]:
        raise ContractError("Validation lifecycle epoch and lease epoch differ")
    if value["state"] not in {
        "LEASED",
        "CLEANING",
        "FAILED_CLEAN",
        "CLEANUP_BLOCKED",
    } and value["capacity"] is None:
        raise ContractError("Validation lifecycle preflight capacity is missing")
    acquired_at = _timestamp(value["lease"]["acquired_at"], "lease acquisition")
    heartbeat_at = _timestamp(value["lease"]["heartbeat_at"], "lease heartbeat")
    expires_at = _timestamp(value["absolute_expires_at"], "absolute expiration")
    last_activity = _timestamp(value["last_activity_at"], "last activity")
    if not acquired_at <= heartbeat_at <= last_activity:
        raise ContractError("Validation lifecycle timestamps are inconsistent")
    if last_activity >= expires_at and value["state"] not in _POST_TTL_STATES:
        raise ContractError("Validation lifecycle exceeded its execution TTL")
    resource_references: set[str] = set()
    for resource in value["resources"]:
        reference = resource["provider_id"]
        if reference in resource_references:
            raise ContractError("Validation resources must have unique provider IDs")
        resource_references.add(reference)
    agents = value["runtime_topology"]["agents"]
    authority_ids = [item["authority_id"] for item in agents]
    names = [item["runtime_agent_name"] for item in agents]
    if len(authority_ids) != len(set(authority_ids)) or len(names) != len(set(names)):
        raise ContractError("Validation runtime topology contains a collision")
    if value["state"] in {
        "VALIDATING",
        "FROZEN",
        "REVIEWED",
        "SHADOW_REVIEW_SKIPPED",
        "REVALIDATING",
        "FINAL_CHECKS",
        "CLEAN",
        "RECEIPT_ISSUED",
    } and len(agents) != 41:
        raise ContractError("Validation runtime topology must contain all 41 Agents")
    if agents and value["digests"]["runtime_topology_digest"] != content_hash(agents):
        raise ContractError("Validation runtime topology digest is stale")
    if value["state"] in {"CLEAN", "FAILED_CLEAN", "RECEIPT_ISSUED"}:
        validate_topology_resource_bindings(
            value["runtime_topology"],
            value["resources"],
        )
        cleanup = value["cleanup"]
        if (
            cleanup["status"] != "exact_clean"
            or cleanup["exact_clean"] is not True
            or cleanup["residue_ids"]
        ):
            raise ContractError("Terminal validation state requires exact cleanup")
        resource_ids = {item["provider_id"] for item in value["resources"]}
        proven_ids = {
            *cleanup["verified_absent_ids"],
            *cleanup["retained_shared_manifest_ids"],
        }
        if resource_ids != proven_ids:
            raise ContractError(
                "Terminal validation state lacks exhaustive resource absence proof"
            )
        verified = set(cleanup["verified_absent_ids"])
        retained = set(cleanup["retained_shared_manifest_ids"])
        for resource in value["resources"]:
            if (
                resource["provider_id"] in verified
                and resource["state"] != "absence_verified"
            ):
                raise ContractError(
                    "Verified-absent validation resource state is inconsistent"
                )
            if (
                resource["provider_id"] in retained
                and resource["kind"] != "acr_manifest"
            ):
                raise ContractError(
                    "Only shared ACR manifests may be retained after cleanup"
                )
    if value["state"] == "CLEANUP_BLOCKED" and (
        value["cleanup"]["status"] != "ambiguous"
        or value["cleanup"]["exact_clean"] is not False
    ):
        raise ContractError("Cleanup-blocked state requires ambiguous cleanup")


class LifecycleJournal:
    def __init__(
        self,
        store: LifecycleStore,
        policy: ValidationPolicy,
        mirror_root: Path | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._mirror_root = mirror_root

    def begin_cycle(
        self,
        initial: Mapping[str, Any],
        *,
        proposed_lease_id: str,
    ) -> tuple[str, BlobRecord]:
        if (
            initial.get("state") != "LEASED"
            or initial.get("snapshot_type") not in {"active", "event"}
            or initial.get("lease", {}).get("lease_id") != proposed_lease_id
        ):
            raise ContractError("Initial validation lifecycle is invalid")
        existing = self._store.read_optional(ACTIVE_CONTAINER, ACTIVE_BLOB)
        event_value = copy.deepcopy(dict(initial))
        event_value["snapshot_type"] = "event"
        event_value["event_snapshot"] = None
        event_value["clean_snapshot"] = None
        event_value["revision"] = 1
        if existing is None:
            event_value["epoch"] = 1
            event_value["lease"]["epoch"] = 1
            event_value["previous_etag"] = None
        else:
            validate_lifecycle(existing.value)
            if existing.value["state"] not in {
                "RECEIPT_ISSUED",
                "FAILED_CLEAN",
            } or existing.value["lease"]["state"] != "released":
                raise ContractError("Validation account already has an active cycle")
            event_value["epoch"] = existing.value["epoch"] + 1
            event_value["lease"]["epoch"] = event_value["epoch"]
            event_value["previous_etag"] = existing.etag
        event_value = stamp_lifecycle_digest(event_value)
        validate_lifecycle(event_value)
        event = self._store.create_once(
            SNAPSHOT_CONTAINER,
            _snapshot_name(event_value, "events"),
            event_value,
        )
        self._mirror_immutable(event)
        active_value = copy.deepcopy(event_value)
        active_value["snapshot_type"] = "active"
        active_value["event_snapshot"] = _snapshot_reference(event)
        active_value = stamp_lifecycle_digest(active_value)
        validate_lifecycle(active_value)
        if existing is None:
            active = self._store.create_once(
                ACTIVE_CONTAINER,
                ACTIVE_BLOB,
                active_value,
            )
            lease_id = self._store.acquire_infinite_lease(
                ACTIVE_CONTAINER,
                ACTIVE_BLOB,
                proposed_lease_id=proposed_lease_id,
            )
            if lease_id != proposed_lease_id:
                raise ContractError("Validation active lease ID is not the proposed ID")
            leased = self._store.read(
                ACTIVE_CONTAINER,
                ACTIVE_BLOB,
                lease_id=lease_id,
            )
            self._mirror_active(leased)
            return lease_id, leased
        lease_id = self._store.acquire_infinite_lease(
            ACTIVE_CONTAINER,
            ACTIVE_BLOB,
            proposed_lease_id=proposed_lease_id,
        )
        if lease_id != proposed_lease_id:
            raise ContractError("Validation active lease ID is not the proposed ID")
        current = self._store.read(
            ACTIVE_CONTAINER,
            ACTIVE_BLOB,
            lease_id=lease_id,
        )
        if current.etag != existing.etag:
            raise ContractError("Validation journal changed before cycle acquisition")
        active = self._store.compare_and_swap(
            ACTIVE_CONTAINER,
            ACTIVE_BLOB,
            active_value,
            lease_id=lease_id,
            etag=current.etag,
        )
        self._mirror_active(active)
        return lease_id, active

    def commit(
        self,
        current: BlobRecord,
        *,
        lease_id: str,
        next_state: str,
        updates: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> LifecycleCommit:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        validate_lifecycle(current.value)
        if current.value["snapshot_type"] != "active":
            raise ContractError("Lifecycle mutation requires the active journal")
        if current.value["lease"]["lease_id"] != lease_id:
            raise ContractError("Lifecycle mutation lease ID does not match")
        self._validate_transition(current.value["state"], next_state)
        previous_heartbeat = _timestamp(
            current.value["lease"]["heartbeat_at"],
            "lease heartbeat",
        )
        if (
            moment - previous_heartbeat
            > timedelta(seconds=self._policy.limits.active_heartbeat_seconds)
            and next_state
            not in {"CLEANING", "CLEAN", "FAILED_CLEAN", "CLEANUP_BLOCKED"}
        ):
            raise ContractError("Validation lifecycle heartbeat is stale")
        if (
            moment
            >= _timestamp(
                current.value["absolute_expires_at"],
                "absolute expiration",
            )
            and next_state not in {"CLEANING", "CLEAN", "FAILED_CLEAN", "CLEANUP_BLOCKED"}
        ):
            raise ContractError("Validation cycle exceeded its absolute TTL")

        event_value = copy.deepcopy(current.value)
        event_value["snapshot_type"] = "event"
        event_value["state"] = next_state
        event_value["revision"] += 1
        event_value["last_activity_at"] = moment.isoformat()
        event_value["previous_etag"] = current.etag
        event_value["event_snapshot"] = None
        if updates:
            _merge(event_value, updates)
        if event_value["absolute_expires_at"] != current.value["absolute_expires_at"]:
            raise ContractError("Validation lifecycle absolute TTL is immutable")
        for key in ("cycle_id", "repository", "pr_number", "epoch"):
            if event_value[key] != current.value[key]:
                raise ContractError(f"Validation lifecycle {key} is immutable")
        for key in ("epoch", "lease_id", "ownership_nonce", "acquired_at"):
            if event_value["lease"][key] != current.value["lease"][key]:
                raise ContractError(f"Validation lifecycle lease {key} is immutable")
        self._protect_project_identity(current.value, event_value)
        event_value["lease"]["heartbeat_at"] = moment.isoformat()
        event_value = stamp_lifecycle_digest(event_value)
        validate_lifecycle(event_value)
        event_name = _snapshot_name(event_value, "events")
        event = self._store.create_once(
            SNAPSHOT_CONTAINER,
            event_name,
            event_value,
        )
        self._mirror_immutable(event)

        clean: BlobRecord | None = None
        active_value = copy.deepcopy(event_value)
        active_value["snapshot_type"] = "active"
        active_value["event_snapshot"] = _snapshot_reference(event)
        if next_state in {"CLEAN", "FAILED_CLEAN"}:
            clean_value = copy.deepcopy(event_value)
            clean_value["snapshot_type"] = "clean"
            clean_value["event_snapshot"] = None
            clean_value["clean_snapshot"] = None
            clean_value = stamp_lifecycle_digest(clean_value)
            validate_lifecycle(clean_value)
            clean = self._store.create_once(
                SNAPSHOT_CONTAINER,
                _snapshot_name(clean_value, "clean"),
                clean_value,
            )
            self._mirror_immutable(clean)
            active_value["clean_snapshot"] = _snapshot_reference(clean)
        active_value = stamp_lifecycle_digest(active_value)
        validate_lifecycle(active_value)
        active = self._store.compare_and_swap(
            ACTIVE_CONTAINER,
            ACTIVE_BLOB,
            active_value,
            lease_id=lease_id,
            etag=current.etag,
        )
        self._mirror_active(active)
        return LifecycleCommit(active=active, event=event, clean=clean)

    def takeover_for_cleanup(
        self,
        *,
        ownership_nonce: str,
        holder_workflow_reference: str,
        holder_app_reference: str,
        holder_run_reference: str,
        now: datetime | None = None,
    ) -> tuple[str, LifecycleCommit]:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        before = self._store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
        validate_lifecycle(before.value)
        if before.value["state"] in {
            "CLEAN",
            "FAILED_CLEAN",
            "RECEIPT_ISSUED",
        }:
            raise ContractError("Clean validation lifecycle needs no takeover")
        heartbeat = _timestamp(
            before.value["lease"]["heartbeat_at"],
            "lease heartbeat",
        )
        expires = _timestamp(
            before.value["absolute_expires_at"],
            "absolute expiration",
        )
        stale_after = timedelta(
            seconds=self._policy.limits.active_heartbeat_seconds
        )
        if moment < expires and moment - heartbeat <= stale_after:
            raise ContractError("Active validation lease is not stale")
        self._store.break_lease(ACTIVE_CONTAINER, ACTIVE_BLOB)
        lease_id = self._store.acquire_infinite_lease(
            ACTIVE_CONTAINER,
            ACTIVE_BLOB,
        )
        current = self._store.read(
            ACTIVE_CONTAINER,
            ACTIVE_BLOB,
            lease_id=lease_id,
        )
        if current.etag != before.etag:
            raise ContractError("Validation journal changed during lease takeover")
        lease = {
            "epoch": current.value["epoch"] + 1,
            "lease_id": lease_id,
            "ownership_nonce": ownership_nonce,
            "holder_workflow_reference": holder_workflow_reference,
            "holder_app_reference": holder_app_reference,
            "holder_run_reference": holder_run_reference,
            "acquired_at": moment.isoformat(),
            "heartbeat_at": moment.isoformat(),
            "state": "held",
        }
        rebased = copy.deepcopy(current.value)
        rebased["epoch"] += 1
        rebased["lease"] = lease
        rebased["state"] = "CLEANING"
        rebased["last_activity_at"] = moment.isoformat()
        rebased = stamp_lifecycle_digest(rebased)
        rebased_record = BlobRecord(
            container=current.container,
            name=current.name,
            value=rebased,
            etag=current.etag,
            version_id=current.version_id,
        )
        return lease_id, self.commit(
            rebased_record,
            lease_id=lease_id,
            next_state="CLEANING",
            now=moment,
        )

    def read_active(self) -> BlobRecord:
        current = self._store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
        validate_lifecycle(current.value)
        return current

    def abandon_expired_clean(
        self,
        *,
        ownership_nonce: str,
        holder_workflow_reference: str,
        holder_app_reference: str,
        holder_run_reference: str,
        now: datetime | None = None,
    ) -> BlobRecord:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        before = self.read_active()
        if before.value["state"] != "CLEAN":
            raise ContractError("Only an unreceipted CLEAN lifecycle can be abandoned")
        if moment < _timestamp(
            before.value["absolute_expires_at"],
            "absolute expiration",
        ):
            raise ContractError("CLEAN receipt handoff has not reached its TTL")
        self._store.break_lease(ACTIVE_CONTAINER, ACTIVE_BLOB)
        lease_id = self._store.acquire_infinite_lease(
            ACTIVE_CONTAINER,
            ACTIVE_BLOB,
        )
        current = self._store.read(
            ACTIVE_CONTAINER,
            ACTIVE_BLOB,
            lease_id=lease_id,
        )
        if current.etag != before.etag:
            raise ContractError("Validation journal changed during terminal takeover")
        lease = {
            "epoch": current.value["epoch"] + 1,
            "lease_id": lease_id,
            "ownership_nonce": ownership_nonce,
            "holder_workflow_reference": holder_workflow_reference,
            "holder_app_reference": holder_app_reference,
            "holder_run_reference": holder_run_reference,
            "acquired_at": moment.isoformat(),
            "heartbeat_at": moment.isoformat(),
            "state": "held",
        }
        rebased = copy.deepcopy(current.value)
        rebased["epoch"] += 1
        rebased["lease"] = lease
        rebased["state"] = "FAILED_CLEAN"
        rebased["last_activity_at"] = moment.isoformat()
        rebased = stamp_lifecycle_digest(rebased)
        rebased_record = BlobRecord(
            container=current.container,
            name=current.name,
            value=rebased,
            etag=current.etag,
            version_id=current.version_id,
        )
        committed = self.commit(
            rebased_record,
            lease_id=lease_id,
            next_state="FAILED_CLEAN",
            updates={
                "failure": {
                    "error_code": "receipt_handoff_expired",
                    "detail_digest": content_hash(
                        {"state": "CLEAN", "cycle_id": current.value["cycle_id"]}
                    ),
                    "failed_at": moment.isoformat(),
                }
            },
            now=moment,
        ).active
        return self.release(committed, lease_id=lease_id, now=moment)

    def complete_receipt_handoff(
        self,
        current: BlobRecord,
        receipt: BlobRecord,
        *,
        lease_id: str,
        now: datetime | None = None,
    ) -> BlobRecord:
        digest = receipt.value.get("receipt_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ContractError("Validation receipt Blob digest is invalid")
        if (
            receipt.value.get("cycle_id") != current.value["cycle_id"]
            or receipt.value.get("epoch") != current.value["epoch"]
        ):
            raise ContractError("Validation receipt belongs to another lifecycle")
        reference = {
            "path": f"{receipt.container}/{receipt.name}",
            "version_id": receipt.version_id,
            "etag": receipt.etag,
            "digest": digest,
        }
        if current.value["state"] == "CLEAN":
            current = self.commit(
                current,
                lease_id=lease_id,
                next_state="RECEIPT_ISSUED",
                updates={"receipt_reference": reference},
                now=now,
            ).active
        elif (
            current.value["state"] != "RECEIPT_ISSUED"
            or current.value["receipt_reference"] != reference
        ):
            raise ContractError(
                "Validation receipt handoff does not match the terminal lifecycle"
            )
        return self.release(current, lease_id=lease_id, now=now)

    def resume_pending_receipt(
        self,
        *,
        now: datetime | None = None,
    ) -> BlobRecord | None:
        current = self.read_active()
        if current.value["state"] != "CLEAN":
            return None
        owner, repository = current.value["repository"].split("/", 1)
        head = current.value["git"]["final_head_sha"]
        if not isinstance(head, str):
            return None
        candidates = (
            (
                "test-agent-validation-receipts",
                (
                    f"receipts/{owner}/{repository}/{current.value['pr_number']}/"
                    f"{head}/test-agent-validation-receipt.json"
                ),
                "merge",
            ),
            (
                "test-agent-validation-shadow-receipts",
                (
                    f"shadow-receipts/{owner}/{repository}/"
                    f"{current.value['pr_number']}/{current.value['cycle_id']}/"
                    f"{head}/test-agent-validation-receipt.json"
                ),
                "shadow",
            ),
        )
        found: list[BlobRecord] = []
        for container, name, mode in candidates:
            record = self._store.read_optional(container, name)
            if record is not None:
                from agent_insights_quality.validation_issuer import (
                    validate_receipt,
                )

                validate_receipt(record.value)
                if record.value["mode"] != mode:
                    raise ContractError("Pending validation receipt mode is invalid")
                found.append(record)
        if len(found) > 1:
            raise ContractError("Multiple pending validation receipts were found")
        if not found:
            return None
        return self.complete_receipt_handoff(
            current,
            found[0],
            lease_id=current.value["lease"]["lease_id"],
            now=now,
        )

    def release(
        self,
        current: BlobRecord,
        *,
        lease_id: str,
        now: datetime | None = None,
    ) -> BlobRecord:
        if current.value["state"] not in {"RECEIPT_ISSUED", "FAILED_CLEAN"}:
            raise ContractError("Validation lease cannot release before a clean terminal state")
        validate_lifecycle(current.value)
        if current.value["lease"]["lease_id"] != lease_id:
            raise ContractError("Validation terminal lease ID does not match")
        if current.value["lease"]["state"] == "held":
            committed = self.commit(
                current,
                lease_id=lease_id,
                next_state=current.value["state"],
                updates={"lease": {"state": "released"}},
                now=now,
            ).active
        elif current.value["lease"]["state"] == "released":
            committed = current
        else:
            raise ContractError("Broken validation lease cannot be released")
        self._store.release_lease(
            ACTIVE_CONTAINER,
            ACTIVE_BLOB,
            lease_id=lease_id,
        )
        return committed

    @staticmethod
    def _validate_transition(current: str, next_state: str) -> None:
        if current not in _TRANSITIONS or next_state not in STATES:
            raise ContractError("Validation lifecycle state is invalid")
        if next_state != current and next_state not in _TRANSITIONS[current]:
            raise ContractError(
                f"Validation lifecycle cannot transition from {current} to {next_state}"
            )

    @staticmethod
    def _protect_project_identity(
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> None:
        prior_name = before["project"]["name"]
        prior_id = before["project"]["provider_id"]
        if prior_name is not None and after["project"]["name"] != prior_name:
            raise ContractError("Validation Project cannot be replaced in one cycle")
        if prior_id is not None and after["project"]["provider_id"] != prior_id:
            raise ContractError("Validation Project cannot be replaced in one cycle")

    def _mirror_immutable(self, record: BlobRecord) -> None:
        if self._mirror_root is not None:
            immutable_json(
                self._mirror_root / record.container / record.name,
                record.value,
            )

    def _mirror_active(self, record: BlobRecord) -> None:
        if self._mirror_root is not None:
            atomic_json(
                self._mirror_root / record.container / record.name,
                record.value,
            )


def _merge(target: dict[str, Any], updates: Mapping[str, Any]) -> None:
    for key, value in updates.items():
        if key not in target:
            raise ContractError(f"Lifecycle update contains unknown field: {key}")
        if isinstance(target[key], dict) and isinstance(value, Mapping):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _snapshot_name(value: Mapping[str, Any], prefix: str) -> str:
    repository = value["repository"].replace("/", "--")
    digest = str(value["journal_digest"]).removeprefix("sha256:")[:16]
    return (
        f"{prefix}/{repository}/{value['pr_number']}/{value['cycle_id']}/"
        f"e{value['epoch']}/r{value['revision']:06d}-"
        f"{value['state'].lower()}-{digest}.json"
    )


def validate_topology_resource_bindings(
    topology: Mapping[str, Any],
    resources: list[Mapping[str, Any]],
) -> None:
    provider_ids = {str(item["provider_id"]) for item in resources}
    required: set[str] = set()
    for field in (
        "project_reference",
        "connection_ids",
        "runtime_principal_ids",
        "telemetry_identity_ids",
    ):
        value = topology.get(field)
        if isinstance(value, str) and value:
            required.add(value)
        elif isinstance(value, list):
            required.update(str(item) for item in value)
    for agent in topology.get("agents", []):
        for field in (
            "provider_agent_id",
            "provider_agent_version_id",
            "hosted_identity_id",
            "hosted_blueprint_id",
            "hosted_deployment_id",
            "runtime_principal_id",
            "telemetry_identity_id",
        ):
            value = agent.get(field)
            if isinstance(value, str) and value:
                required.add(value)
        required.update(str(item) for item in agent.get("connection_ids", []))
    missing = sorted(required - provider_ids)
    if missing:
        raise ContractError(
            "Validation runtime topology has unjournaled provider identities: "
            + ", ".join(missing)
        )


def _snapshot_reference(record: BlobRecord) -> dict[str, str]:
    return {
        "path": f"{record.container}/{record.name}",
        "version_id": record.version_id,
        "etag": record.etag,
        "digest": record.value["journal_digest"],
    }


def _timestamp(value: str, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ContractError(f"Validation {label} timestamp is invalid") from error
    if result.tzinfo is None:
        raise ContractError(f"Validation {label} timestamp lacks a timezone")
    return result.astimezone(UTC)
