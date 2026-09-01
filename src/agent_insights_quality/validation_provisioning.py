from __future__ import annotations

import copy
import json
import subprocess
import threading
import time
import urllib.parse
import uuid
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
    content_hash,
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
class ProjectResourcePlan:
    intents: tuple[dict[str, str | None], ...]
    connection_ids: tuple[str, ...]
    role_assignment_ids: tuple[str, ...]
    role_assignment_names: tuple[str, ...]


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
    cycle_id: str,
    base: RuntimeProfile | None = None,
) -> RuntimeProfile:
    source = base or RuntimeProfile.from_env("staging", "g29")
    if (
        not source.account_name
        or not source.account_resource_id
        or source.telemetry_resource_set != "g29"
    ):
        raise ContractError("Validation requires the existing staging Foundry account")
    return source.with_project(
        name=f"validation-{cycle_id}",
        project_name=project_name,
        registry_path=validation_runtime_root()
        / cycle_id
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
        self._profile.assert_insights_connection(
            "application-insights-validation",
            account_connection_name="application-insights-staging",
        )

    def assert_project_absent(self, project_name: str) -> None:
        project_id = self.expected_project_id(project_name)
        process = subprocess.run(
            [
                azure_cli(),
                "resource",
                "show",
                "--ids",
                project_id,
                "--output",
                "none",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if process.returncode == 0:
            raise ContractError("Target validation Project already exists")
        if process.returncode != 3:
            raise ContractError("Target validation Project absence is ambiguous")

    def resource_intents(
        self,
        *,
        project_name: str,
        cycle_id: str,
        ownership_nonce: str,
    ) -> tuple[dict[str, str | None], ...]:
        return self._resource_plan(
            project_name=project_name,
            cycle_id=cycle_id,
            ownership_nonce=ownership_nonce,
        ).intents

    def create(
        self,
        *,
        project_name: str,
        cycle_id: str,
        ownership_nonce: str,
    ) -> ProjectDeployment:
        if not self._policy.project_name_policy.accepts(project_name):
            raise ContractError("Validation Project name violates policy")
        application_insights_name = _resource_name(
            self._profile.application_insights_resource_id
        )
        if (
            not self._profile.account_name
            or not self._profile.container_registry_name
            or not application_insights_name
        ):
            raise ContractError("Validation fixed Azure substrate is incomplete")
        plan = self._resource_plan(
            project_name=project_name,
            cycle_id=cycle_id,
            ownership_nonce=ownership_nonce,
        )
        deployment_name = _deployment_name(cycle_id)
        command = [
            azure_cli(),
            "deployment",
            "group",
            "create",
            "--resource-group",
            RESOURCE_GROUP,
            "--name",
            deployment_name,
            "--template-file",
            str(ROOT / "infra" / "modules" / "validation-project.bicep"),
            "--parameters",
            "location=westus2",
            f"accountName={self._profile.account_name}",
            f"projectName={project_name}",
            f"applicationInsightsName={application_insights_name}",
            f"registryName={self._profile.container_registry_name}",
            f"validationOperatorPrincipalId={self._local_operator_id}",
            f"ownershipNonce={ownership_nonce}",
            f"cycleId={cycle_id}",
            f"validationOperatorProjectManagerName={plan.role_assignment_names[0]}",
            f"appInsightsReaderName={plan.role_assignment_names[1]}",
            f"modelInferenceUserName={plan.role_assignment_names[2]}",
            f"registryPullName={plan.role_assignment_names[3]}",
            "--only-show-errors",
            "--output",
            "json",
        ]
        try:
            with self._progress.heartbeat(
                "ephemeral validation Project create"
            ) as outcome:
                process = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=30 * 60,
                    check=False,
                )
                if process.returncode != 0:
                    outcome.fail()
        except (subprocess.TimeoutExpired, OSError) as error:
            self._cancel_and_wait_deployment(deployment_name)
            raise ContractError(
                "Ephemeral validation Project deployment did not complete locally"
            ) from error
        if process.returncode != 0:
            self._cancel_and_wait_deployment(deployment_name)
            raise ContractError("Ephemeral validation Project creation failed")
        try:
            deployment = json.loads(process.stdout)
            deployment_state = deployment["properties"]["provisioningState"]
            outputs = deployment["properties"]["outputs"]
            project = ProjectDeployment(
                project_name=project_name,
                project_id=str(outputs["projectId"]["value"]),
                project_principal_id=str(outputs["projectPrincipalId"]["value"]),
                project_endpoint=str(outputs["projectEndpoint"]["value"]),
                connection_ids=tuple(outputs["connectionIds"]["value"]),
                role_assignment_ids=tuple(outputs["roleAssignmentIds"]["value"]),
                resource_observations=(),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ContractError(
                "Ephemeral validation Project deployment outputs are invalid"
            ) from error
        if deployment_state != "Succeeded":
            self._cancel_and_wait_deployment(deployment_name)
            raise ContractError(
                "Ephemeral validation Project deployment is not terminal-success"
            )
        if (
            project.project_endpoint != self._profile.project_endpoint
            or len(project.connection_ids) != 2
            or len(project.role_assignment_ids) != 4
            or set(project.connection_ids) != set(plan.connection_ids)
            or set(project.role_assignment_ids) != set(plan.role_assignment_ids)
        ):
            raise ContractError("Ephemeral validation Project topology is incomplete")
        observations = [
            (
                str(
                    next(
                        item["intent_reference"]
                        for item in plan.intents
                        if item["kind"] == "arm_deployment"
                    )
                ),
                str(
                    next(
                        item["discovery_key"]
                        for item in plan.intents
                        if item["kind"] == "arm_deployment"
                    )
                ),
            ),
            (
                str(
                    next(
                        item["intent_reference"]
                        for item in plan.intents
                        if item["kind"] == "runtime_principal"
                    )
                ),
                project.project_principal_id,
            ),
            *(
                (
                    str(item["intent_reference"]),
                    str(item["discovery_key"]),
                )
                for item in plan.intents
                if item["kind"] in {"connection", "role_assignment"}
            ),
        ]
        return ProjectDeployment(
            **{
                **project.__dict__,
                "resource_observations": tuple(observations),
            }
        )

    def _cancel_and_wait_deployment(self, deployment_name: str) -> None:
        terminal = {"Succeeded", "Failed", "Canceled"}
        for attempt in range(31):
            state = subprocess.run(
                [
                    azure_cli(),
                    "deployment",
                    "group",
                    "show",
                    "--resource-group",
                    RESOURCE_GROUP,
                    "--name",
                    deployment_name,
                    "--query",
                    "properties.provisioningState",
                    "--output",
                    "tsv",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if state.returncode == 3:
                return
            if state.returncode != 0:
                raise ContractError(
                    "Validation deployment terminal state is ambiguous"
                )
            status = state.stdout.strip()
            if status in terminal:
                return
            if attempt == 0:
                cancel = subprocess.run(
                    [
                        azure_cli(),
                        "deployment",
                        "group",
                        "cancel",
                        "--resource-group",
                        RESOURCE_GROUP,
                        "--name",
                        deployment_name,
                        "--output",
                        "none",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if cancel.returncode not in {0, 3}:
                    raise ContractError(
                        "Validation deployment cancellation failed"
                    )
            if attempt < 30:
                time.sleep(5)
        raise ContractError("Validation deployment did not reach a terminal state")

    def _resource_plan(
        self,
        *,
        project_name: str,
        cycle_id: str,
        ownership_nonce: str,
    ) -> ProjectResourcePlan:
        if (
            not self._profile.account_resource_id
            or not self._profile.application_insights_resource_id
            or not self._profile.container_registry_name
        ):
            raise ContractError("Validation fixed Azure substrate is incomplete")
        project_id = self.expected_project_id(project_name)
        account_id = self._profile.account_resource_id.rstrip("/")
        insights_id = self._profile.application_insights_resource_id.rstrip("/")
        registry_id = (
            f"{account_id.split('/providers/', 1)[0]}/providers/"
            "Microsoft.ContainerRegistry/registries/"
            f"{self._profile.container_registry_name}"
        )
        connection_ids = (
            f"{project_id}/connections/container-registry-validation",
            f"{project_id}/connections/application-insights-validation",
        )
        role_scopes = (
            project_id,
            insights_id,
            account_id,
            registry_id,
        )
        role_labels = (
            "validation-operator-project-manager",
            "application-insights-reader",
            "model-inference-user",
            "registry-pull",
        )
        role_names = tuple(
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    (
                        "test-agent-validation:"
                        f"{cycle_id}:{ownership_nonce}:{label}:{scope}"
                    ),
                )
            )
            for label, scope in zip(role_labels, role_scopes, strict=True)
        )
        role_ids = tuple(
            f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"
            for scope, name in zip(role_scopes, role_names, strict=True)
        )
        identity_intent = content_hash(
            {
                "kind": "runtime_principal",
                "project_id": project_id,
                "cycle_id": cycle_id,
                "ownership_nonce": ownership_nonce,
            }
        )
        deployment_name = _deployment_name(cycle_id)
        deployment_id = (
            f"{self._profile.account_resource_id.split('/providers/', 1)[0]}"
            "/providers/Microsoft.Resources/deployments/"
            f"{deployment_name}"
        )
        intents: list[dict[str, str | None]] = [
            {
                "kind": "arm_deployment",
                "intent_reference": content_hash(
                    {"kind": "arm_deployment", "provider_id": deployment_id}
                ),
                "deterministic_name": deployment_name,
                "parent_id": self._profile.account_resource_id.split(
                    "/providers/",
                    1,
                )[0],
                "authority_id": None,
                "cleanup_method": "explicit",
                "runtime_kind": "control",
                "discovery_key": deployment_id,
            },
            {
                "kind": "runtime_principal",
                "intent_reference": identity_intent,
                "deterministic_name": f"{project_name}-system-identity",
                "parent_id": project_id,
                "authority_id": None,
                "cleanup_method": "documented_project_cascade",
                "runtime_kind": "control",
                "discovery_key": project_id,
            }
        ]
        intents.extend(
            {
                "kind": "connection",
                "intent_reference": content_hash(
                    {"kind": "connection", "provider_id": provider_id}
                ),
                "deterministic_name": provider_id.rsplit("/", 1)[-1],
                "parent_id": project_id,
                "authority_id": None,
                "cleanup_method": "explicit",
                "runtime_kind": "control",
                "discovery_key": provider_id,
            }
            for provider_id in connection_ids
        )
        intents.extend(
            {
                "kind": "role_assignment",
                "intent_reference": content_hash(
                    {"kind": "role_assignment", "provider_id": provider_id}
                ),
                "deterministic_name": label,
                "parent_id": scope,
                "authority_id": None,
                "cleanup_method": "explicit",
                "runtime_kind": "control",
                "discovery_key": provider_id,
            }
            for provider_id, label, scope in zip(
                role_ids,
                role_labels,
                role_scopes,
                strict=True,
            )
        )
        return ProjectResourcePlan(
            intents=tuple(intents),
            connection_ids=connection_ids,
            role_assignment_ids=role_ids,
            role_assignment_names=role_names,
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
    cycle_id: str,
    progress: ProgressReporter | None = None,
    record_resource: Callable[[dict[str, Any]], None] | None = None,
) -> ValidationSupportImages:
    reporter = progress or ProgressReporter("aiq-validation-images")
    images = _build_support_images(
        profile,
        dict(support_agent),
        progress=reporter,
        record_resource=None,
    )
    journaled_manifest_ids: set[str] = set()
    resources: list[dict[str, str | None]] = []
    repository = "agent-insights-quality-support"
    for logical_version, image in sorted(images.items()):
        prefix, separator, digest = image.rpartition("@")
        if (
            not separator
            or not prefix.endswith(f"/{repository}")
            or not digest.startswith("sha256:")
        ):
            raise ContractError(
                "Validation Support image is not digest pinned"
            )
        tag = _cycle_image_tag(cycle_id, logical_version)
        authority_id = (
            "support-ticket-agent/v0"
            if logical_version == "v0"
            else logical_version
        )
        tag_resource = {
            "kind": "acr_tag",
            "intent_reference": content_hash(
                {
                    "kind": "acr_tag",
                    "provider_id": f"{repository}:{tag}",
                    "authority_id": authority_id,
                }
            ),
            "deterministic_name": f"{repository}:{tag}",
            "provider_id": f"{repository}:{tag}",
            "authority_id": authority_id,
            "parent_id": digest,
            "runtime_kind": "hosted_custom_container",
            "discovery_key": f"{repository}:{tag}",
        }
        if record_resource is not None:
            record_resource({**tag_resource, "state": "create_intent"})
        with reporter.heartbeat(
            f"support-ticket-agent/{logical_version}: cycle tag"
        ) as outcome:
            process = subprocess.run(
                [
                    azure_cli(),
                    "acr",
                    "import",
                    "--name",
                    profile.container_registry_name,
                    "--source",
                    image,
                    "--image",
                    f"{repository}:{tag}",
                    "--force",
                    "--output",
                    "none",
                ],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if process.returncode != 0:
                outcome.fail()
        if process.returncode != 0:
            if record_resource is not None:
                record_resource(
                    {**tag_resource, "state": "ambiguous_create"}
                )
            raise ContractError(
                f"Validation Support cycle tag failed for {logical_version}"
            )
        if record_resource is not None:
            record_resource({**tag_resource, "state": "created"})
            new_manifest = digest not in journaled_manifest_ids
            if new_manifest:
                manifest_intent = content_hash(
                    {
                        "kind": "acr_manifest",
                        "provider_id": digest,
                        "authority_id": authority_id,
                    }
                )
                manifest_resource = {
                    "kind": "acr_manifest",
                    "deterministic_name": repository,
                    "authority_id": authority_id,
                    "parent_id": None,
                    "intent_reference": manifest_intent,
                    "runtime_kind": "hosted_custom_container",
                    "discovery_key": f"{repository}@{digest}",
                }
                record_resource(
                    {**manifest_resource, "state": "create_intent"}
                )
                record_resource(
                    {
                        **manifest_resource,
                        "state": "created",
                        "provider_id": digest,
                    }
                )
                journaled_manifest_ids.add(digest)
        else:
            new_manifest = digest not in journaled_manifest_ids
            journaled_manifest_ids.add(digest)
        resources.append(tag_resource)
        if new_manifest:
            resources.append(
                {
                    "kind": "acr_manifest",
                    "deterministic_name": repository,
                    "provider_id": digest,
                    "authority_id": authority_id,
                    "parent_id": None,
                }
            )
    return ValidationSupportImages(
        images=images,
        resources=tuple(resources),
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
                fallback_agent_id=(
                    deployed.provider_agent_id if not hosted else None
                ),
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
                "Validation phase 2 requires nine Support images"
            )
        if self._support_images and self._support_images != values:
            raise ContractError(
                "Validation Support image topology changed"
            )
        self._support_images = values

    def deploy(
        self,
        authority: AuthoritySpec,
        planned: PlannedRuntime,
    ) -> DeployedRuntime:
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
        runtime_agent = copy.deepcopy(catalog_agent)
        runtime_agent["name"] = planned.runtime_agent_name
        version, details = (
            self._client.ensure_version_for_readiness(
                agent=runtime_agent,
                logical_version=authority.logical_version,
                artifact=artifact,
            )
        )
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
                fallback_agent_id=provider_agent_id,
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
        deployed = DeployedRuntime(
            authority_id=authority.authority_id,
            runtime_kind=authority.runtime_kind,
            runtime_agent_name=planned.runtime_agent_name,
            runtime_agent_version=version,
            provider_agent_id=provider_agent_id,
            provider_agent_version_id=proof.provider_version_id,
            hosted_identity_id=hosted_identity_id if hosted else None,
            hosted_blueprint_id=hosted_blueprint_id if hosted else None,
            hosted_deployment_id=hosted_deployment_id if hosted else None,
            runtime_principal_id=runtime_principal_id or None,
            telemetry_identity_id=proof.provider_version_id,
            connection_ids=self._project.connection_ids,
        )
        self._remember_readiness_proof(authority, deployed, proof)
        return deployed


def _resource_name(resource_id: str) -> str:
    parsed = urllib.parse.urlparse(resource_id)
    value = parsed.path if parsed.scheme else resource_id
    return value.rstrip("/").rsplit("/", 1)[-1]


def _deployment_name(cycle_id: str) -> str:
    return f"test-agent-validation-{cycle_id}"[:64].rstrip("-")


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
    fallback_agent_id: str | None = None,
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
        or (
            f"{fallback_agent_id}/versions/{expected_version}"
            if fallback_agent_id
            else ""
        )
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


def _cycle_image_tag(cycle_id: str, logical_version: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in f"{cycle_id}-{logical_version}".casefold()
    ).strip("-")
    if not normalized or len(normalized) > 128:
        raise ContractError("Validation ACR cycle tag violates provider limits")
    return f"validation-{normalized}"
