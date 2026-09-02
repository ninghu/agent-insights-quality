from __future__ import annotations

import copy
import json
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.profiles import RESOURCE_GROUP, RuntimeProfile
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.provisioning import FoundryProvisioner, build_artifact
from agent_insights_quality.provisioning import _build_support_images
from agent_insights_quality.util import (
    ROOT,
    ContractError,
)
from agent_insights_quality.validation_lifecycle import validation_runtime_root
from agent_insights_quality.validation_policy import ValidationPolicy
from agent_insights_quality.validation_manifest import source_content_digest
from agent_insights_quality.validation_runtime import (
    AuthoritySpec,
    DeployedRuntime,
    PlannedRuntime,
)
from agent_insights_quality.validation_quota import CapacityMeasurement


HOSTED_VALIDATION_OUTPUT_TELEMETRY_ENVIRONMENT = {
    # Each hosted framework has its own documented synthetic-content opt-in.
    "ENABLE_SENSITIVE_DATA": "true",
    "AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED": "true",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
}


def _build_validation_artifact(
    catalog_agent: dict[str, Any],
    issue: dict[str, Any] | None,
    *,
    support_images: Mapping[str, str],
) -> dict[str, Any]:
    return build_artifact(
        catalog_agent,
        issue,
        support_images=support_images,
        hosted_environment_variables=(
            HOSTED_VALIDATION_OUTPUT_TELEMETRY_ENVIRONMENT
            if catalog_agent["type"] != "prompt"
            else None
        ),
    )


@dataclass(frozen=True)
class ProjectDeployment:
    project_name: str
    project_id: str
    project_principal_id: str
    project_endpoint: str
    connection_ids: tuple[str, ...]
    role_assignment_ids: tuple[str, ...]
    resource_observations: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ValidationSupportImages:
    images: dict[str, str]
    resources: tuple[dict[str, str | None], ...]


@dataclass(frozen=True)
class HostedVersionTopology:
    agent_id: str
    version_id: str
    identity_id: str
    blueprint_id: str
    deployment_id: str
    runtime_principal_id: str


@dataclass(frozen=True)
class _VersionReadinessProof:
    provider_version_id: str
    hosted_topology: HostedVersionTopology | None


def validation_runtime_profile(
    project_name: str,
    *,
    run_id: str,
    base: RuntimeProfile | None = None,
) -> RuntimeProfile:
    source = base or RuntimeProfile.from_env("staging", "g30")
    if (
        not source.account_name
        or not source.account_resource_id
        or source.telemetry_resource_set != "g30"
        or source.environment_id != "swedencentral-g30"
        or source.location != "swedencentral"
        or source.account_name != "aiq-staging-swedencentral"
        or source.project_name != "aiq-staging-swedencentral"
        or project_name != source.project_name
    ):
        raise ContractError("Validation requires the reviewed durable Sweden staging Project")
    return source.with_project(
        name="staging",
        project_name=project_name,
        registry_path=validation_runtime_root()
        / run_id
        / "deployment-registry.json",
    )


class ValidationProjectProvisioner:
    def __init__(
        self,
        profile: RuntimeProfile,
        *,
        local_operator_id: str,
        policy: ValidationPolicy,
        progress: ProgressReporter | None = None,
    ) -> None:
        if not local_operator_id:
            raise ContractError("Local validation operator identity is required")
        self._profile = profile
        self._local_operator_id = local_operator_id
        self._policy = policy
        self._progress = progress or ProgressReporter("aiq-validation-project")

    def expected_project_id(self, project_name: str) -> str:
        if not self._profile.account_resource_id:
            raise ContractError("Validation account resource identity is missing")
        return (
            f"{self._profile.account_resource_id.rstrip('/')}/projects/"
            f"{project_name}"
        )

    def assert_test_agent_model(self, expected: dict[str, str]) -> None:
        self._profile.assert_test_agent_model(expected)

    def assert_telemetry_connection(self) -> None:
        self._profile.assert_insights_connection("application-insights-staging")

    def bind(self, project_name: str) -> ProjectDeployment:
        if (
            project_name != self._policy.project_name
            or project_name != self._profile.project_name
            or project_name != self._profile.account_name
        ):
            raise ContractError("Validation Project binding is not the reviewed durable Project")
        project_id = self.expected_project_id(project_name)
        try:
            with self._progress.heartbeat("durable validation Project binding") as outcome:
                process = subprocess.run(
                    [
                        azure_cli(),
                        "resource",
                        "show",
                        "--ids",
                        project_id,
                        "--output",
                        "json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if process.returncode != 0:
                    outcome.fail()
        except (subprocess.TimeoutExpired, OSError) as error:
            raise ContractError(
                "Durable validation Project binding did not complete locally"
            ) from error
        if process.returncode != 0:
            raise ContractError("Durable validation Project could not be read")
        expected_project_name = f"{self._profile.account_name}/{project_name}"
        try:
            value = json.loads(process.stdout)
            principal_id = str(value["identity"]["principalId"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ContractError(
                "Durable validation Project response is invalid"
            ) from error
        if (
            str(value.get("id") or "").casefold() != project_id.casefold()
            or value.get("name") != expected_project_name
            or str(value.get("location") or "").casefold() != self._policy.location
            or not principal_id
        ):
            raise ContractError("Durable validation Project identity is invalid")
        self.assert_telemetry_connection()
        connection_ids = (
            f"{project_id}/connections/container-registry-staging",
            f"{project_id}/connections/application-insights-staging",
        )
        return ProjectDeployment(
            project_name=project_name,
            project_id=project_id,
            project_principal_id=principal_id,
            project_endpoint=self._profile.project_endpoint,
            connection_ids=connection_ids,
            role_assignment_ids=(),
            resource_observations=(),
        )


def measure_test_agent_capacity(
    profile: RuntimeProfile,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CapacityMeasurement:
    if not profile.account_name:
        raise ContractError("Validation Foundry account name is unavailable")
    process = subprocess.run(
        [
            azure_cli(),
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "--name",
            profile.account_name,
            "--resource-group",
            RESOURCE_GROUP,
            "--deployment-name",
            "gpt-5.4-mini",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        raise ContractError("Validation model capacity could not be measured")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ContractError("Validation model capacity response is invalid") from error
    rpm, tpm = _rate_limits(value)
    return CapacityMeasurement(
        rpm=rpm,
        tpm=tpm,
        measured_at=now().astimezone(UTC).isoformat(),
    )


def prepare_validation_support_images(
    profile: RuntimeProfile,
    support_agent: Mapping[str, Any],
    *,
    progress: ProgressReporter | None = None,
) -> ValidationSupportImages:
    reporter = progress or ProgressReporter("aiq-validation-images")
    images = _build_support_images(
        profile,
        dict(support_agent),
        progress=reporter,
        record_resource=None,
    )
    if len(images) != 9 or any(
        "@sha256:" not in image for image in images.values()
    ):
        raise ContractError("Validation Support images are not exactly digest pinned")
    return ValidationSupportImages(
        images=images,
        resources=(),
    )


class FoundryAuthorityDeployer:
    def __init__(
        self,
        *,
        profile: RuntimeProfile,
        agent_catalog: Mapping[str, Any],
        issue_catalog: Mapping[str, Any],
        token_provider: Callable[[str], str],
        project: ProjectDeployment,
        support_images: Mapping[str, str] | None = None,
        progress: ProgressReporter | None = None,
    ) -> None:
        self._profile = profile
        self._agents = {
            item["name"]: item for item in agent_catalog["agents"]
        }
        self._issues = {
            item["id"]: item for item in issue_catalog["issues"]
        }
        self._project = project
        self._support_images = dict(support_images or {})
        self._client = FoundryProvisioner(
            profile,
            token_provider=token_provider,
            progress=progress or ProgressReporter("aiq-validation-deploy"),
        )
        self._readiness_lock = threading.Lock()
        self._readiness_proofs: dict[
            tuple[str, str, str, str, str, str], _VersionReadinessProof
        ] = {}

    def wait_project(self) -> None:
        self._client.wait_project()

    def assert_no_monitors(self) -> None:
        if self._client._list_monitors():
            raise ContractError("Validation Project must contain zero monitors")

    def assert_ready(
        self,
        authority: AuthoritySpec,
        deployed: DeployedRuntime,
    ) -> None:
        hosted = authority.runtime_kind != "prompt"
        proof = self._take_readiness_proof(authority, deployed)
        if proof is None:
            details = self._client.version_details(
                deployed.runtime_agent_name,
                deployed.runtime_agent_version,
                hosted=hosted,
            )
            proof = _version_readiness_proof(
                details,
                hosted=hosted,
                expected_agent_name=deployed.runtime_agent_name,
                expected_version=deployed.runtime_agent_version,
            )
            metadata = details.get("metadata")
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("aiq_content_digest")
                != deployed.provider_content_digest
            ):
                raise ContractError(
                    "Validation canary Agent content digest changed"
                )
        topology = proof.hosted_topology
        if proof.provider_version_id != deployed.provider_agent_version_id:
            raise ContractError(
                "Validation canary Agent version identity changed"
            )
        if hosted and (
            topology is None
            or topology.identity_id != deployed.hosted_identity_id
            or topology.blueprint_id != deployed.hosted_blueprint_id
            or topology.deployment_id != deployed.hosted_deployment_id
            or topology.runtime_principal_id != deployed.runtime_principal_id
        ):
            raise ContractError(
                "Validation Hosted canary topology is not ready"
            )

    @staticmethod
    def _readiness_key(
        authority: AuthoritySpec,
        deployed: DeployedRuntime,
    ) -> tuple[str, str, str, str, str, str]:
        return (
            authority.authority_id,
            authority.runtime_kind,
            deployed.authority_id,
            deployed.runtime_kind,
            deployed.runtime_agent_name,
            deployed.runtime_agent_version,
        )

    def _remember_readiness_proof(
        self,
        authority: AuthoritySpec,
        deployed: DeployedRuntime,
        proof: _VersionReadinessProof,
    ) -> None:
        with self._readiness_lock:
            self._readiness_proofs[
                self._readiness_key(authority, deployed)
            ] = proof

    def _take_readiness_proof(
        self,
        authority: AuthoritySpec,
        deployed: DeployedRuntime,
    ) -> _VersionReadinessProof | None:
        lock = getattr(self, "_readiness_lock", None)
        if lock is None:
            return None
        with lock:
            return self._readiness_proofs.pop(
                self._readiness_key(authority, deployed),
                None,
            )

    def set_support_images(self, images: Mapping[str, str]) -> None:
        values = dict(images)
        if len(values) != 9:
            raise ContractError(
                "Hosted validation requires nine Support images"
            )
        if self._support_images and self._support_images != values:
            raise ContractError(
                "Validation Support image topology changed"
            )
        self._support_images = values

    def desired_content_digest(self, authority: AuthoritySpec) -> str:
        _, artifact = self._validation_artifact(authority)
        return str(artifact["content_digest"])

    def find_existing(
        self,
        authority: AuthoritySpec,
        planned: PlannedRuntime,
    ) -> DeployedRuntime | None:
        _, artifact = self._validation_artifact(authority)
        hosted = authority.runtime_kind != "prompt"
        version = self._client._find_version(
            planned.runtime_agent_name,
            authority.logical_version,
            artifact["content_digest"],
            hosted=hosted,
        )
        if version is None:
            return None
        details = self._client._wait_active(
            planned.runtime_agent_name,
            version,
            hosted=hosted,
            expected_metadata={
                "aiq_profile": self._profile.name,
                "aiq_logical_version": authority.logical_version,
                "aiq_content_digest": artifact["content_digest"],
            },
            not_found_confirmed_at=None,
        )
        return self._runtime_from_details(
            authority,
            planned,
            artifact=artifact,
            version=version,
            details=details,
        )

    def deploy(
        self,
        authority: AuthoritySpec,
        planned: PlannedRuntime,
    ) -> DeployedRuntime:
        catalog_agent, artifact = self._validation_artifact(authority)
        runtime_agent = copy.deepcopy(catalog_agent)
        runtime_agent["name"] = planned.runtime_agent_name
        version, details = self._client.ensure_version_for_readiness(
            agent=runtime_agent,
            logical_version=authority.logical_version,
            artifact=artifact,
        )
        deployed = self._runtime_from_details(
            authority,
            planned,
            artifact=artifact,
            version=version,
            details=details,
        )
        self._remember_readiness_proof(
            authority,
            deployed,
            _version_readiness_proof(
                details,
                hosted=authority.runtime_kind != "prompt",
                expected_agent_name=planned.runtime_agent_name,
                expected_version=version,
            ),
        )
        return deployed

    def _runtime_from_details(
        self,
        authority: AuthoritySpec,
        planned: PlannedRuntime,
        *,
        artifact: Mapping[str, Any],
        version: str,
        details: Mapping[str, Any],
    ) -> DeployedRuntime:
        hosted = authority.runtime_kind != "prompt"
        if hosted:
            proof = _version_readiness_proof(
                details,
                hosted=True,
                expected_agent_name=planned.runtime_agent_name,
                expected_version=version,
            )
            topology = proof.hosted_topology
            assert topology is not None
            provider_agent_id = topology.agent_id
            hosted_identity_id = topology.identity_id
            hosted_blueprint_id = topology.blueprint_id
            hosted_deployment_id = topology.deployment_id
            runtime_principal_id = topology.runtime_principal_id
        else:
            provider_agent_id = str(
                details.get("agent_id")
                or details.get("agentId")
                or planned.runtime_agent_name
            )
            proof = _version_readiness_proof(
                details,
                hosted=False,
                expected_agent_name=planned.runtime_agent_name,
                expected_version=version,
            )
            hosted_identity_id = None
            hosted_blueprint_id = None
            hosted_deployment_id = None
            identity = details.get("identity")
            runtime_principal_id = (
                str(
                    identity.get("principal_id")
                    or identity.get("principalId")
                    or ""
                )
                if isinstance(identity, dict)
                else ""
            )
        return DeployedRuntime(
            authority_id=authority.authority_id,
            runtime_kind=authority.runtime_kind,
            runtime_agent_name=planned.runtime_agent_name,
            runtime_agent_version=version,
            provider_agent_id=provider_agent_id,
            provider_agent_version_id=proof.provider_version_id,
            provider_content_digest=str(artifact["content_digest"]),
            hosted_identity_id=hosted_identity_id if hosted else None,
            hosted_blueprint_id=hosted_blueprint_id if hosted else None,
            hosted_deployment_id=hosted_deployment_id if hosted else None,
            runtime_principal_id=runtime_principal_id or None,
            telemetry_identity_id=proof.provider_version_id,
            connection_ids=self._project.connection_ids,
        )
    def _validation_artifact(
        self,
        authority: AuthoritySpec,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        catalog_agent = self._agents.get(authority.canonical_agent)
        if catalog_agent is None:
            raise ContractError("Validation authority Agent is not in the catalog")
        issue = (
            None
            if authority.authority_kind == "baseline"
            else self._issues.get(authority.authority_id)
        )
        if authority.authority_kind == "issue" and issue is None:
            raise ContractError("Validation issue authority is not in the catalog")
        artifact = _build_validation_artifact(
            catalog_agent,
            issue,
            support_images=self._support_images,
        )
        root = ROOT / (
            catalog_agent["baseline_path"]
            if issue is None
            else issue["implementation"]
        )
        if (
            source_content_digest(root, authority.runtime_kind)
            != authority.source_content_digest
        ):
            raise ContractError("Validation authority source digest changed before deploy")
        return dict(catalog_agent), artifact


def _resource_name(resource_id: str) -> str:
    parsed = urllib.parse.urlparse(resource_id)
    value = parsed.path if parsed.scheme else resource_id
    return value.rstrip("/").rsplit("/", 1)[-1]


def _deployment_name(run_id: str) -> str:
    return f"test-agent-validation-{run_id}"[:64].rstrip("-")


def _required_mapping(
    value: Mapping[str, Any],
    field: str,
) -> Mapping[str, Any]:
    nested = value.get(field)
    if not isinstance(nested, Mapping):
        raise ContractError(
            f"Hosted Agent version topology field is invalid: {field}"
        )
    return nested


def _required_string(
    value: Mapping[str, Any],
    field: str,
    *,
    path: str,
) -> str:
    result = value.get(field)
    if (
        not isinstance(result, str)
        or not result
        or result != result.strip()
    ):
        raise ContractError(
            f"Hosted Agent version topology field is invalid: {path}"
        )
    return result


def _version_readiness_proof(
    details: Mapping[str, Any],
    *,
    hosted: bool,
    expected_agent_name: str,
    expected_version: str,
) -> _VersionReadinessProof:
    if hosted:
        topology = _hosted_version_topology(
            details,
            expected_agent_name=expected_agent_name,
            expected_version=expected_version,
        )
        return _VersionReadinessProof(
            provider_version_id=topology.version_id,
            hosted_topology=topology,
        )
    if str(details.get("status") or "").casefold() != "active":
        raise ContractError("Validation canary Agent version is not active")
    provider_version_id = str(
        details.get("id")
        or details.get("version_id")
    )
    if not provider_version_id:
        raise ContractError(
            "Validation canary Agent version identity is missing"
        )
    return _VersionReadinessProof(
        provider_version_id=provider_version_id,
        hosted_topology=None,
    )


def _hosted_version_topology(
    details: Mapping[str, Any],
    *,
    expected_agent_name: str,
    expected_version: str,
) -> HostedVersionTopology:
    if details.get("status") != "active":
        raise ContractError("Validation canary Agent version is not active")
    if details.get("object") != "agent.version":
        raise ContractError(
            "Hosted Agent version topology field is invalid: object"
        )
    agent_id = _required_string(
        details,
        "name",
        path="name",
    )
    version = _required_string(
        details,
        "version",
        path="version",
    )
    if agent_id != expected_agent_name or version != expected_version:
        raise ContractError("Validation canary Agent version identity changed")
    version_id = _required_string(
        details,
        "id",
        path="id",
    )
    instance_identity = _required_mapping(details, "instance_identity")
    runtime_principal_id = _required_string(
        instance_identity,
        "principal_id",
        path="instance_identity.principal_id",
    )
    identity_id = _required_string(
        instance_identity,
        "client_id",
        path="instance_identity.client_id",
    )
    if (
        "status" in instance_identity
        and instance_identity["status"] != "active"
    ):
        raise ContractError(
            "Hosted Agent version topology field is invalid: "
            "instance_identity.status"
        )
    blueprint = _required_mapping(details, "blueprint")
    _required_string(
        blueprint,
        "principal_id",
        path="blueprint.principal_id",
    )
    _required_string(
        blueprint,
        "client_id",
        path="blueprint.client_id",
    )
    if "status" in blueprint and blueprint["status"] != "active":
        raise ContractError(
            "Hosted Agent version topology field is invalid: blueprint.status"
        )
    blueprint_reference = _required_mapping(details, "blueprint_reference")
    if blueprint_reference.get("type") != "ManagedAgentIdentityBlueprint":
        raise ContractError(
            "Hosted Agent version topology field is invalid: "
            "blueprint_reference.type"
        )
    blueprint_id = _required_string(
        blueprint_reference,
        "blueprint_id",
        path="blueprint_reference.blueprint_id",
    )
    deployment_id = _required_string(
        details,
        "agent_guid",
        path="agent_guid",
    )
    return HostedVersionTopology(
        agent_id=agent_id,
        version_id=version_id,
        identity_id=identity_id,
        blueprint_id=blueprint_id,
        deployment_id=deployment_id,
        runtime_principal_id=runtime_principal_id,
    )


def _rate_limits(value: Any) -> tuple[int, int]:
    candidates: list[Mapping[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in {"rateLimits", "rate_limits"} and isinstance(
                    child,
                    list,
                ):
                    candidates.extend(
                        value for value in child if isinstance(value, Mapping)
                    )
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    rates: dict[str, int] = {}
    for item in candidates:
        label = str(
            item.get("key")
            or item.get("name")
            or item.get("type")
            or ""
        ).casefold()
        count = item.get("count") or item.get("limit")
        seconds = item.get("renewalPeriodInSeconds") or item.get(
            "renewal_period_seconds"
        )
        if (
            isinstance(count, bool)
            or not isinstance(count, (int, float))
            or count <= 0
        ):
            continue
        period = 60.0
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            if seconds <= 0:
                continue
            period = float(seconds)
        per_minute = int(float(count) * 60.0 / period)
        if "token" in label:
            rates["tpm"] = max(rates.get("tpm", 0), per_minute)
        elif "request" in label:
            rates["rpm"] = max(rates.get("rpm", 0), per_minute)
    if rates.get("rpm", 0) <= 0 or rates.get("tpm", 0) <= 0:
        raise ContractError(
            "Validation model response lacks measured RPM/TPM rate limits"
        )
    return rates["rpm"], rates["tpm"]
