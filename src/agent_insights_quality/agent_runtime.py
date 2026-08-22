from __future__ import annotations

import hashlib
import json
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from agent_insights_quality.contracts import ContractError
from agent_insights_quality.source_artifacts import reviewed_source_files


API_VERSION = "v1"
HOSTED_FEATURES = "HostedAgents=V1Preview"
OWNER_KEY = "agent_insights_quality_owner"
RUN_KEY = "agent_insights_quality_run_id"
DIGEST_KEY = "agent_insights_quality_artifact_digest"
SOURCE_DIGEST_KEY = "agent_insights_quality_source_digest"
IMAGE_DIGEST_KEY = "agent_insights_quality_image_digest"
OWNER_VALUE = "agent-insights-quality"
TERMINAL_FAILURE_STATES = frozenset({"failed", "deleted", "deleting"})
FORBIDDEN_INGESTION_HOSTS = frozenset(
    {
        "monitor.azure.com",
        "applicationinsights.azure.com",
        "dc.applicationinsights.azure.com",
    }
)
_AGENT_NAME = re.compile(r"^aiq-[0-9]{3}-[a-z][a-z0-9-]*(?:-[a-z0-9]+)*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACR_TICKET_REPOSITORY = re.compile(
    r"^[a-z0-9]{5,50}\.azurecr\.io/agent-insights-quality-ticket$"
)
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")

# Reviewed endpoint-faults catalog - mirrors scenario_runtime._ENDPOINT_CASES so that
# prompt-agent scenario_operations can produce the same structured tool-call output as
# hosted agents without re-defining case semantics in individual tests.
_ENDPOINT_CASES: dict[str, dict[str, Any]] = {
    "guardrail-bypass-probe": {
        "case": "guardrail-bypass-probe",
        "status": "guardrail_triggered",
    },
    "no-confirmation": {
        "case": "no-confirmation",
        "status": "action_without_confirmation",
    },
    "malformed-approval": {
        "case": "malformed-approval",
        "status": "malformed_approval",
    },
    "cross-account-synthetic-record": {
        "case": "cross-account-synthetic-record",
        "status": "cross_account_access",
    },
    "correlated-child-failure": {
        "case": "correlated-child-failure",
        "child": {"status": "failed"},
        "parent": {"status": "ok"},
        "status": "nested_failure",
    },
    "zero-token-outer-successful-child": {
        "case": "zero-token-outer-successful-child",
        "child": {"status": "ok"},
        "parent": {"tokens": 0},
        "status": "ok",
    },
    "handled-child-failure": {
        "case": "handled-child-failure",
        "child": {"status": "failed"},
        "parent": {"status": "recovered"},
        "status": "ok",
    },
}
# Healthy controls dispatch normally and wrap the result; the others return the case
# definition directly (no dispatch).
_ENDPOINT_HEALTHY_CASES: frozenset[str] = frozenset(
    {"zero-token-outer-successful-child", "handled-child-failure"}
)


class RuntimeContractError(ContractError):
    """Raised when deployment or endpoint traffic violates the runtime contract."""


class FoundryRequestTimeout(RuntimeContractError):
    code = "agent_deployment_timeout"
    transient = True


class DeploymentCleanupError(RuntimeContractError):
    code = "deployment_cleanup_sessions_active"
    transient = True


class DeploymentPollError(RuntimeContractError):
    """Raised after creation when a concrete version fails to become usable."""

    def __init__(
        self,
        message: str,
        receipt: DeploymentReceipt,
        *,
        code: str = "agent_deployment_failed",
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.receipt = receipt
        self.code = code
        self.transient = transient


@dataclass(frozen=True, slots=True)
class InvocationFailureReceipt:
    """Preserves whatever IDs are available when a hosted endpoint call fails."""

    agent_name: str
    agent_version: str
    http_status: int
    response_id: str | None = None
    invocation_id: str | None = None
    request_id: str | None = None
    session_id: str | None = None


class InvocationEndpointError(RuntimeContractError):
    """A sanitized prompt/session endpoint failure with retry-safety metadata."""

    def __init__(
        self,
        message: str,
        receipt: InvocationFailureReceipt,
        *,
        code: str = "endpoint_invocation_failed",
        transient: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.receipt = receipt
        self.code = code
        self.transient = transient
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Mapping[str, Any]:
        try:
            value = json.loads(self.body)
        except json.JSONDecodeError as error:
            raise RuntimeContractError("Foundry returned invalid JSON.") from error
        if not isinstance(value, Mapping):
            raise RuntimeContractError("Foundry returned a non-object JSON response.")
        return value


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Small synchronous transport with bounded calls and no credential logging."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status_code=error.code,
                headers=dict(error.headers.items()) if error.headers else {},
                body=error.read(),
            )


@dataclass(frozen=True, slots=True)
class DeploymentReceipt:
    agent_name: str
    agent_version: str
    agent_type: str
    artifact_digest: str
    run_id: str
    status: str
    source_digest: str | None = None
    image_digest: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupReceipt:
    agent_name: str
    agent_version: str
    run_id: str
    deleted: bool


@dataclass(frozen=True, slots=True)
class InvocationReceipt:
    fixture_id: str
    agent_name: str
    agent_version: str
    response_id: str
    invocation_id: str | None
    request_id: str | None
    session_id: str | None
    output_text: str
    called_tools: tuple[str, ...]
    response_ids: tuple[str, ...] = ()

    @property
    def trace_id(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class SyntheticToolOperation:
    """One ordered tool-call step for prompt-agent synthetic traffic scenarios.

    When a :class:`HealthyFixture` carries ``scenario_operations``, each tool
    call the agent makes consumes the next operation in sequence: the declared
    ``result`` is returned verbatim (or derived from ``endpoint_case``),
    ``delay_seconds`` is slept before the response is handed back, and the
    tool name is verified against the agent's actual call so deviations fail
    immediately.

    When ``endpoint_case`` is set it must be one of the seven reviewed
    endpoint-faults catalog values.  For healthy cases the ``result`` is
    wrapped with the case metadata; for unhealthy cases the case definition is
    used directly and ``result`` is ignored.
    """

    tool_name: str
    result: Mapping[str, Any]
    delay_seconds: float = 0.0
    endpoint_case: str | None = None


@dataclass(frozen=True, slots=True)
class HealthyFixture:
    id: str
    input: str
    output_contains: str
    tool_outputs: Mapping[str, Mapping[str, Any]]
    expected_tool_calls: tuple[str, ...]
    validate_output: bool = True
    validate_tools: bool = True
    scenario_operations: tuple[SyntheticToolOperation, ...] | None = None
    expected_final_status: str = "completed"


@dataclass(frozen=True, slots=True)
class LiveSpanEvidence:
    operation_id: str
    span_id: str
    parent_span_id: str | None
    observed_at: datetime
    kind: str
    name: str
    tool_name: str | None = None
    tool_arguments: Mapping[str, Any] | None = None
    tool_result: Any | None = None


@dataclass(frozen=True, slots=True)
class LiveTelemetryEvidence:
    run_id: str
    agent_id: str
    agent_name: str
    agent_version: str
    fixture_id: str
    response_id: str
    invocation_id: str | None
    request_id: str | None
    session_id: str | None
    operation_id: str
    observed_at: datetime
    spans: tuple[LiveSpanEvidence, ...]


def validate_telemetry_identifiers(evidence: LiveTelemetryEvidence) -> None:
    if (
        not _OPERATION_ID.fullmatch(evidence.operation_id)
        or evidence.operation_id == "0" * 32
    ):
        raise RuntimeContractError(
            "Live telemetry operation IDs must be 32-character lowercase hexadecimal values."
        )
    span_ids = {span.span_id for span in evidence.spans}
    if len(span_ids) != len(evidence.spans) or any(
        not _SPAN_ID.fullmatch(span_id) or span_id == "0" * 16
        for span_id in span_ids
    ):
        raise RuntimeContractError(
            "Live telemetry span IDs must be unique 16-character lowercase hexadecimal values."
        )
    for span in evidence.spans:
        if (
            span.operation_id != evidence.operation_id
            or not _OPERATION_ID.fullmatch(span.operation_id)
            or span.operation_id == "0" * 32
        ):
            raise RuntimeContractError(
                "Live telemetry spans must belong to the enclosing operation ID."
            )
        if span.parent_span_id is not None and (
            not _SPAN_ID.fullmatch(span.parent_span_id)
            or span.parent_span_id == "0" * 16
        ):
            raise RuntimeContractError(
                "Live telemetry parent span IDs must be 16-character lowercase hexadecimal values."
            )


def validate_deployment_receipt(receipt: DeploymentReceipt) -> None:
    _validate_agent_name(receipt.agent_name)
    if (
        not receipt.agent_version
        or not receipt.run_id
        or len(receipt.run_id) > 64
        or receipt.agent_type
        not in {"prompt", "hosted_code", "hosted_custom_container"}
        or not _DIGEST.fullmatch(receipt.artifact_digest)
        or (
            receipt.source_digest is not None
            and not _DIGEST.fullmatch(receipt.source_digest)
        )
        or (
            receipt.image_digest is not None
            and not _DIGEST.fullmatch(receipt.image_digest)
        )
        or (
            receipt.agent_type == "prompt"
            and (receipt.source_digest is not None or receipt.image_digest is not None)
        )
        or (
            receipt.agent_type == "hosted_code"
            and (receipt.source_digest is None or receipt.image_digest is not None)
        )
        or (
            receipt.agent_type == "hosted_custom_container"
            and (receipt.image_digest is None or receipt.source_digest is not None)
        )
    ):
        raise RuntimeContractError("Live qualification deployment receipt is malformed.")


def sha256_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def canonical_json_digest(value: Mapping[str, Any]) -> str:
    content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    return sha256_digest(content)


def deterministic_zip(source: Path) -> tuple[bytes, str]:
    if not source.is_dir():
        raise RuntimeContractError(f"Hosted source directory does not exist: {source}")
    paths = reviewed_source_files(source)
    if not paths:
        raise RuntimeContractError("Hosted source directory is empty.")
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in paths:
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes())
    content = output.getvalue()
    return content, sha256_digest(content)


def load_fixtures(path: Path) -> tuple[HealthyFixture, ...]:
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeContractError(f"Invalid healthy traffic fixture: {path}") from error
    if not isinstance(value, list) or len(value) < 3:
        raise RuntimeContractError("Each healthy traffic fixture must contain at least three tasks.")
    fixtures: list[HealthyFixture] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise RuntimeContractError("Healthy traffic tasks must be objects.")
        required = {
            "id",
            "input",
            "output_contains",
            "tool_outputs",
            "expected_tool_calls",
        }
        if set(item) != required:
            raise RuntimeContractError(
                f"Healthy traffic task fields must be exactly {sorted(required)}."
            )
        tool_outputs = item["tool_outputs"]
        expected_calls = item["expected_tool_calls"]
        if (
            not isinstance(tool_outputs, Mapping)
            or not all(isinstance(result, Mapping) for result in tool_outputs.values())
            or not isinstance(expected_calls, list)
            or not all(isinstance(name, str) and name for name in expected_calls)
            or set(expected_calls) != set(tool_outputs)
        ):
            raise RuntimeContractError("Healthy traffic tool contracts are invalid.")
        fixtures.append(
            HealthyFixture(
                id=str(item["id"]),
                input=str(item["input"]),
                output_contains=str(item["output_contains"]),
                tool_outputs={str(name): dict(result) for name, result in tool_outputs.items()},
                expected_tool_calls=tuple(str(name) for name in expected_calls),
            )
        )
    ids = [fixture.id for fixture in fixtures]
    if len(ids) != len(set(ids)):
        raise RuntimeContractError("Healthy traffic fixture IDs must be unique.")
    return tuple(fixtures)


def validate_image_reference(image: str) -> str:
    repository, separator, digest = image.partition("@")
    allowed_repository = (
        repository == "ghcr.io/ninghu/agent-insights-quality-ticket"
        or _ACR_TICKET_REPOSITORY.fullmatch(repository) is not None
    )
    if separator != "@" or not allowed_repository or not _DIGEST.fullmatch(digest):
        raise RuntimeContractError(
            "Ticket image must use the reviewed GHCR or Azure Container Registry repository "
            "pinned by sha256 digest."
        )
    return image


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _multipart_body(
    metadata: Mapping[str, Any],
    archive_name: str,
    archive: bytes,
    artifact_digest: str,
) -> tuple[str, bytes]:
    boundary = "aiq-" + artifact_digest.removeprefix("sha256:")[:24]
    chunks = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        _json_bytes(metadata),
        b"\r\n",
        f"--{boundary}\r\n".encode(),
        (
            f'Content-Disposition: form-data; name="code"; filename="{archive_name}"\r\n'
        ).encode(),
        b"Content-Type: application/zip\r\n\r\n",
        archive,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return boundary, b"".join(chunks)


class FoundryDeploymentClient:
    def __init__(
        self,
        project_endpoint: str,
        token_provider: Callable[[], str],
        *,
        transport: HttpTransport | None = None,
        request_timeout_seconds: float = 300,
        poll_timeout_seconds: float = 1800,
        poll_interval_seconds: float = 5,
        code_error_grace_seconds: float | None = None,
        code_error_required_observations: int = 2,
        cleanup_retry_attempts: int = 4,
        cleanup_retry_interval_seconds: float = 300,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            request_timeout_seconds <= 0
            or poll_timeout_seconds <= 0
            or poll_interval_seconds < 0
            or (
                code_error_grace_seconds is not None
                and (
                    code_error_grace_seconds < 0
                    or code_error_grace_seconds > poll_timeout_seconds
                )
            )
            or code_error_required_observations < 2
            or code_error_required_observations > 10
            or cleanup_retry_attempts <= 0
            or cleanup_retry_interval_seconds < 0
        ):
            raise RuntimeContractError(
                "Deployment timeouts, intervals, and cleanup attempts must be bounded and valid."
            )
        self._endpoint = _validate_project_endpoint(project_endpoint)
        self._token_provider = token_provider
        self._transport = transport or UrllibTransport()
        self._request_timeout = request_timeout_seconds
        self._poll_timeout = poll_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._code_error_grace = (
            min(900, poll_timeout_seconds)
            if code_error_grace_seconds is None
            else code_error_grace_seconds
        )
        self._code_error_observations = code_error_required_observations
        self._cleanup_attempts = cleanup_retry_attempts
        self._cleanup_interval = cleanup_retry_interval_seconds
        self._sleep = sleeper
        self._monotonic = monotonic

    def recover_version(
        self,
        *,
        agent_name: str,
        agent_type: str,
        run_id: str,
        artifact_digest: str,
        source_digest: str | None = None,
        image_digest: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[DeploymentReceipt | None, bool]:
        """Reconcile a deterministic deployment after a process interruption."""
        _validate_agent_name(agent_name)
        response = self._request(
            "GET",
            f"/agents/{_quote(agent_name)}",
            headers=(
                {"Foundry-Features": HOSTED_FEATURES}
                if agent_type != "prompt"
                else {}
            ),
            body=None,
            expected_statuses={200, 404},
        )
        if response.status_code == 404:
            return None, False
        payload = response.json()
        candidates: list[Mapping[str, Any]] = []
        if payload.get("version"):
            candidates.append(payload)
        versions = payload.get("versions")
        if isinstance(versions, Mapping):
            latest = versions.get("latest")
            if isinstance(latest, Mapping):
                candidates.append(latest)
            values = versions.get("value") or versions.get("data")
            if isinstance(values, list):
                candidates.extend(
                    item for item in values if isinstance(item, Mapping)
                )
        elif isinstance(versions, list):
            candidates.extend(
                item for item in versions if isinstance(item, Mapping)
            )
        expected = _ownership_metadata(
            run_id,
            artifact_digest,
            source_digest=source_digest,
            image_digest=image_digest,
        )
        matches = [
            candidate
            for candidate in candidates
            if isinstance(candidate.get("metadata"), Mapping)
            and all(
                candidate["metadata"].get(key) == value
                for key, value in expected.items()
            )
        ]
        unique = {
            str(candidate.get("version") or ""): candidate
            for candidate in matches
            if candidate.get("version")
        }
        if len(unique) > 1:
            raise RuntimeContractError(
                "Foundry returned multiple versions for one immutable deployment identity."
            )
        if not unique:
            return None, True
        version = next(iter(unique))
        return (
            self._poll(
                agent_name,
                version,
                agent_type,
                run_id,
                artifact_digest,
                source_digest=source_digest,
                image_digest=image_digest,
                cancelled=cancelled,
            ),
            True,
        )

    def deploy_prompt(
        self,
        *,
        agent_name: str,
        definition: Mapping[str, Any],
        run_id: str,
        create_agent: bool = True,
        cancelled: Callable[[], bool] | None = None,
    ) -> DeploymentReceipt:
        _validate_agent_name(agent_name)
        artifact_digest = canonical_json_digest(definition)
        body: dict[str, Any] = {
            "definition": definition,
            "metadata": _ownership_metadata(run_id, artifact_digest),
        }
        if create_agent:
            body["name"] = agent_name
        return self._deploy_json(
            agent_name,
            "prompt",
            body,
            run_id,
            artifact_digest,
            hosted=False,
            create_agent=create_agent,
            cancelled=cancelled,
        )

    def deploy_hosted_source(
        self,
        *,
        agent_name: str,
        definition: Mapping[str, Any],
        source: Path,
        run_id: str,
        create_agent: bool = True,
        cancelled: Callable[[], bool] | None = None,
    ) -> DeploymentReceipt:
        _validate_agent_name(agent_name)
        archive, source_digest = deterministic_zip(source)
        artifact_digest = canonical_json_digest(
            {"definition": definition, "source_digest": source_digest}
        )
        metadata = {
            "definition": definition,
            "metadata": _ownership_metadata(
                run_id,
                artifact_digest,
                source_digest=source_digest,
            ),
        }
        boundary, body = _multipart_body(
            metadata, f"{agent_name}.zip", archive, source_digest
        )
        path = "/agents" if create_agent else f"/agents/{_quote(agent_name)}/versions"
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "x-ms-code-zip-sha256": source_digest.removeprefix("sha256:"),
            "Foundry-Features": HOSTED_FEATURES,
        }
        if create_agent:
            headers["x-ms-agent-name"] = agent_name
        response = self._request(
            "POST",
            path,
            headers=headers,
            body=body,
        )
        version = _version_from_response(response)
        return self._poll(
            agent_name,
            version,
            "hosted_code",
            run_id,
            artifact_digest,
            source_digest=source_digest,
            cancelled=cancelled,
        )

    def deploy_hosted_container(
        self,
        *,
        agent_name: str,
        definition: Mapping[str, Any],
        image: str,
        run_id: str,
        create_agent: bool = True,
        cancelled: Callable[[], bool] | None = None,
    ) -> DeploymentReceipt:
        _validate_agent_name(agent_name)
        pinned_image = validate_image_reference(image)
        resolved = json.loads(json.dumps(definition))
        container = resolved.get("container_configuration")
        if not isinstance(container, dict):
            raise RuntimeContractError("Container definition has no container_configuration.")
        container["image"] = pinned_image
        image_digest = pinned_image.partition("@")[2]
        artifact_digest = canonical_json_digest(resolved)
        body: dict[str, Any] = {
            "definition": resolved,
            "metadata": _ownership_metadata(
                run_id,
                artifact_digest,
                image_digest=image_digest,
            ),
        }
        if create_agent:
            body["name"] = agent_name
        return self._deploy_json(
            agent_name,
            "hosted_custom_container",
            body,
            run_id,
            artifact_digest,
            hosted=True,
            create_agent=create_agent,
            image_digest=image_digest,
            cancelled=cancelled,
        )

    def cleanup_version(self, receipt: DeploymentReceipt) -> CleanupReceipt:
        validate_deployment_receipt(receipt)
        current_response = self._request(
            "GET",
            f"/agents/{_quote(receipt.agent_name)}/versions/{_quote(receipt.agent_version)}",
            headers=(
                {"Foundry-Features": HOSTED_FEATURES}
                if receipt.agent_type != "prompt"
                else {}
            ),
            body=None,
            expected_statuses={200, 404},
        )
        if current_response.status_code == 404:
            return CleanupReceipt(
                agent_name=receipt.agent_name,
                agent_version=receipt.agent_version,
                run_id=receipt.run_id,
                deleted=True,
            )
        current = current_response.json()
        metadata = current.get("metadata")
        expected = _ownership_metadata(
            receipt.run_id,
            receipt.artifact_digest,
            source_digest=receipt.source_digest,
            image_digest=receipt.image_digest,
        )
        if not isinstance(metadata, Mapping) or any(
            metadata.get(key) != value for key, value in expected.items()
        ):
            raise RuntimeContractError(
                "Cleanup refused because the deployed version ownership does not match."
            )
        hosted = receipt.agent_type != "prompt"
        response: HttpResponse | None = None
        for attempt in range(1, self._cleanup_attempts + 1):
            response = self._request(
                "DELETE",
                f"/agents/{_quote(receipt.agent_name)}/versions/{_quote(receipt.agent_version)}",
                headers={"Foundry-Features": HOSTED_FEATURES} if hosted else {},
                body=None,
                expected_statuses={200, 404, 409} if hosted else {200, 404},
            )
            if response.status_code == 404:
                return CleanupReceipt(
                    agent_name=receipt.agent_name,
                    agent_version=receipt.agent_version,
                    run_id=receipt.run_id,
                    deleted=True,
                )
            if response.status_code != 409:
                break
            if attempt == self._cleanup_attempts:
                raise DeploymentCleanupError(
                    "Hosted agent cleanup remains blocked by active sessions after bounded retries."
                )
            self._sleep(self._cleanup_interval)
        if response is None:
            raise AssertionError("unreachable")
        result = response.json()
        if (
            str(result.get("name") or "") != receipt.agent_name
            or str(result.get("version") or "") != receipt.agent_version
            or result.get("deleted") is not True
        ):
            raise RuntimeContractError(
                "Foundry did not confirm deletion of the exact owned agent version."
            )
        return CleanupReceipt(
            agent_name=receipt.agent_name,
            agent_version=receipt.agent_version,
            run_id=receipt.run_id,
            deleted=True,
        )

    def _deploy_json(
        self,
        agent_name: str,
        agent_type: str,
        body: Mapping[str, Any],
        run_id: str,
        artifact_digest: str,
        *,
        hosted: bool,
        create_agent: bool,
        source_digest: str | None = None,
        image_digest: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> DeploymentReceipt:
        path = "/agents" if create_agent else f"/agents/{_quote(agent_name)}/versions"
        response = self._request_json("POST", path, json_body=body, hosted=hosted)
        version = _version_from_mapping(response)
        return self._poll(
            agent_name,
            version,
            agent_type,
            run_id,
            artifact_digest,
            source_digest=source_digest,
            image_digest=image_digest,
            cancelled=cancelled,
        )

    def _poll(
        self,
        agent_name: str,
        version: str,
        agent_type: str,
        run_id: str,
        artifact_digest: str,
        *,
        source_digest: str | None = None,
        image_digest: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> DeploymentReceipt:
        deadline = self._monotonic() + self._poll_timeout
        code_error_deadline: float | None = None
        consecutive_code_errors = 0
        last_status = "creating"

        def provisional(status: str) -> DeploymentReceipt:
            return DeploymentReceipt(
                agent_name=agent_name,
                agent_version=version,
                agent_type=agent_type,
                artifact_digest=artifact_digest,
                run_id=run_id,
                status=status,
                source_digest=source_digest,
                image_digest=image_digest,
            )

        while True:
            if cancelled is not None and cancelled():
                raise DeploymentPollError(
                    "Agent version polling was cancelled.",
                    provisional(last_status),
                    code="agent_deployment_cancelled",
                )
            try:
                response = self._request_json(
                    "GET",
                    f"/agents/{_quote(agent_name)}/versions/{_quote(version)}",
                    hosted=agent_type != "prompt",
                )
            except RuntimeContractError as error:
                raise DeploymentPollError(
                    "Agent version status polling failed after creation.",
                    provisional("poll_error"),
                    code=getattr(error, "code", "agent_deployment_failed"),
                    transient=bool(getattr(error, "transient", False)),
                ) from error
            status = str(response.get("status") or "").casefold()
            last_status = status or last_status
            if status == "active":
                metadata = response.get("metadata")
                expected = _ownership_metadata(
                    run_id,
                    artifact_digest,
                    source_digest=source_digest,
                    image_digest=image_digest,
                )
                if not isinstance(metadata, Mapping) or any(
                    metadata.get(key) != value for key, value in expected.items()
                ):
                    raise DeploymentPollError(
                        "Active agent version does not preserve immutable ownership metadata.",
                        provisional("active_unverified"),
                    )
                receipt = provisional(status)
                if agent_type != "prompt":
                    try:
                        self._verify_hosted_session_binding(receipt)
                    except RuntimeContractError as error:
                        raise DeploymentPollError(
                            "Active hosted agent version failed exact-version session validation.",
                            receipt,
                            code="agent_deployment_validation_failed",
                        ) from error
                return receipt
            if status in TERMINAL_FAILURE_STATES:
                error = response.get("error")
                detail = str(error.get("code") if isinstance(error, Mapping) else status)
                if status == "failed" and detail.casefold() == "codeerror":
                    now = self._monotonic()
                    if code_error_deadline is None:
                        code_error_deadline = min(
                            deadline,
                            now + self._code_error_grace,
                        )
                    consecutive_code_errors += 1
                    if (
                        consecutive_code_errors >= self._code_error_observations
                        and now >= code_error_deadline
                    ):
                        raise DeploymentPollError(
                            "Agent version remained in terminal state 'failed' "
                            "(CodeError) through the stabilization grace.",
                            provisional(status),
                        )
                    self._poll_sleep(
                        self._poll_interval,
                        cancelled,
                        provisional(status),
                    )
                    continue
                raise DeploymentPollError(
                    f"Agent version reached terminal state '{status}' ({detail}).",
                    provisional(status),
                )
            code_error_deadline = None
            consecutive_code_errors = 0
            if self._monotonic() >= deadline:
                raise DeploymentPollError(
                    "Agent version did not become active before timeout.",
                    provisional("timeout"),
                    code="agent_deployment_poll_timeout",
                    transient=True,
                )
            self._poll_sleep(
                self._poll_interval,
                cancelled,
                provisional(status or last_status),
            )

    def _verify_hosted_session_binding(self, receipt: DeploymentReceipt) -> None:
        response = self._request(
            "POST",
            f"/agents/{_quote(receipt.agent_name)}/endpoint/sessions",
            headers={
                "Content-Type": "application/json",
                "Foundry-Features": HOSTED_FEATURES,
            },
            body=_json_bytes(
                {
                    "version_indicator": {
                        "type": "version_ref",
                        "agent_version": receipt.agent_version,
                    }
                }
            ),
        )
        session = response.json()
        session_id = _first_text(session, "agent_session_id", "session_id", "id")
        indicator = session.get("version_indicator")
        indicator_type = (
            str(indicator.get("type") or "")
            if isinstance(indicator, Mapping)
            else ""
        )
        resolved_version = (
            str(indicator.get("agent_version") or "")
            if isinstance(indicator, Mapping)
            else ""
        )
        if not session_id:
            raise RuntimeContractError(
                "Active hosted version did not create an exact-version validation session."
            )
        try:
            if (
                indicator_type != "version_ref"
                or resolved_version != receipt.agent_version
            ):
                raise RuntimeContractError(
                    "Active hosted version validation session resolved a different version."
                )
        finally:
            self._request(
                "DELETE",
                f"/agents/{_quote(receipt.agent_name)}/endpoint/sessions/"
                f"{_quote(session_id)}",
                headers={"Foundry-Features": HOSTED_FEATURES},
                body=None,
                expected_statuses={200, 202, 204, 404},
            )

    def _poll_sleep(
        self,
        seconds: float,
        cancelled: Callable[[], bool] | None,
        receipt: DeploymentReceipt,
    ) -> None:
        remaining = seconds
        while remaining > 0:
            if cancelled is not None and cancelled():
                raise DeploymentPollError(
                    "Agent version polling was cancelled.",
                    receipt,
                    code="agent_deployment_cancelled",
                )
            interval = min(1.0, remaining)
            self._sleep(interval)
            remaining -= interval

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        hosted: bool = False,
    ) -> Mapping[str, Any]:
        headers = {"Content-Type": "application/json"} if json_body is not None else {}
        if hosted:
            headers["Foundry-Features"] = HOSTED_FEATURES
        return self._request(
            method,
            path,
            headers=headers,
            body=_json_bytes(json_body) if json_body is not None else None,
        ).json()

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        expected_statuses: set[int] | None = None,
    ) -> HttpResponse:
        token = self._token_provider().strip()
        if not token or token == "******":
            raise RuntimeContractError("Token provider returned no usable access token.")
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **headers,
        }
        separator = "&" if "?" in path else "?"
        try:
            response = self._transport.request(
                method,
                f"{self._endpoint}{path}{separator}api-version={API_VERSION}",
                headers=request_headers,
                body=body,
                timeout_seconds=self._request_timeout,
            )
        except TimeoutError as error:
            raise FoundryRequestTimeout(
                f"Foundry deployment request exceeded the bounded "
                f"{self._request_timeout:g}-second timeout."
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise FoundryRequestTimeout(
                    f"Foundry deployment request exceeded the bounded "
                    f"{self._request_timeout:g}-second timeout."
                ) from error
            raise RuntimeContractError("Foundry deployment transport failed.") from error
        allowed = expected_statuses or {200, 201, 202}
        if response.status_code not in allowed:
            raise RuntimeContractError(
                f"Foundry request failed with HTTP {response.status_code}."
            )
        return response


class FoundryInvocationClient:
    def __init__(
        self,
        project_endpoint: str,
        token_provider: Callable[[], str],
        *,
        transport: HttpTransport | None = None,
        request_timeout_seconds: float = 120,
        max_tool_turns: int = 4,
        transient_retry_attempts: int = 3,
        transient_retry_interval_seconds: float = 60,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            request_timeout_seconds <= 0
            or max_tool_turns <= 0
            or transient_retry_attempts <= 0
            or transient_retry_attempts > 5
            or transient_retry_interval_seconds < 0
            or transient_retry_interval_seconds > 300
        ):
            raise RuntimeContractError(
                "Invocation timeouts, turns, and retry settings must be bounded and valid."
            )
        self._endpoint = _validate_project_endpoint(project_endpoint)
        self._token_provider = token_provider
        self._transport = transport or UrllibTransport()
        self._request_timeout = request_timeout_seconds
        self._max_tool_turns = max_tool_turns
        self._transient_retry_attempts = transient_retry_attempts
        self._transient_retry_interval = transient_retry_interval_seconds
        self._sleep = sleeper

    def invoke_prompt(
        self,
        receipt: DeploymentReceipt,
        fixture: HealthyFixture,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> InvocationReceipt:
        if receipt.agent_type != "prompt":
            raise RuntimeContractError("Prompt invocation requires a prompt deployment.")
        reference = {
            "type": "agent_reference",
            "name": receipt.agent_name,
            "version": receipt.agent_version,
        }
        self._require_not_cancelled(cancelled)
        raw_response = self._invoke_response(
            receipt,
            fixture,
            "/openai/v1/responses",
            {"input": fixture.input, "store": True, "agent_reference": reference},
            cancelled=cancelled,
        )
        response = raw_response.json()
        initial_response_id = str(response.get("id") or "")
        if not initial_response_id:
            raise RuntimeContractError("Prompt response omitted its response ID.")
        response_ids = [initial_response_id]
        called_tools: list[str] = []
        op_index = 0
        for _ in range(self._max_tool_turns):
            self._require_not_cancelled(cancelled)
            calls = [
                item
                for item in response.get("output", [])
                if isinstance(item, Mapping) and item.get("type") == "function_call"
            ]
            if not calls:
                if fixture.scenario_operations is not None and op_index != len(
                    fixture.scenario_operations
                ):
                    raise RuntimeContractError(
                        "Prompt agent returned fewer tool calls than configured operations."
                    )
                return _invocation_receipt(
                    receipt,
                    fixture,
                    response,
                    _invocation_id(response),
                    _request_id(raw_response),
                    None,
                    tuple(called_tools),
                    tuple(response_ids),
                    validate_tools=fixture.validate_tools,
                )
            outputs = []
            for call in calls:
                name = str(call.get("name") or "")
                call_id = str(call.get("call_id") or "")
                if not name or not call_id:
                    raise RuntimeContractError("Prompt agent returned an incomplete tool call.")
                if fixture.scenario_operations is not None:
                    if op_index >= len(fixture.scenario_operations):
                        raise RuntimeContractError(
                            "Prompt agent made more tool calls than configured operations."
                        )
                    operation = fixture.scenario_operations[op_index]
                    op_index += 1
                    if name != operation.tool_name:
                        raise RuntimeContractError(
                            f"Tool sequence mismatch at position {op_index - 1}: "
                            f"expected '{operation.tool_name}', agent called '{name}'."
                        )
                    if operation.delay_seconds > 0:
                        self._interruptible_sleep(operation.delay_seconds, cancelled)
                    if operation.endpoint_case is not None:
                        case_def = _ENDPOINT_CASES.get(operation.endpoint_case)
                        if case_def is None:
                            raise RuntimeContractError(
                                f"Unknown endpoint_case value: {operation.endpoint_case!r}. "
                                f"Must be one of: {sorted(_ENDPOINT_CASES)}."
                            )
                        if operation.endpoint_case in _ENDPOINT_HEALTHY_CASES:
                            dispatch_result = json.dumps(
                                dict(operation.result), sort_keys=True, separators=(",", ":")
                            )
                            result_output = json.dumps(
                                {"dispatch_result": dispatch_result, **case_def},
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        else:
                            result_output = json.dumps(
                                case_def, sort_keys=True, separators=(",", ":")
                            )
                    else:
                        result_output = json.dumps(
                            dict(operation.result), sort_keys=True, separators=(",", ":")
                        )
                else:
                    raw_arguments = str(call.get("arguments") or "")
                    if not raw_arguments:
                        raise RuntimeContractError("Prompt agent returned an incomplete tool call.")
                    try:
                        arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError as error:
                        raise RuntimeContractError(
                            "Prompt agent returned invalid tool arguments."
                        ) from error
                    configured = fixture.tool_outputs.get(name)
                    if configured is None:
                        raise RuntimeContractError(
                            f"Prompt agent called unexpected tool '{name}'."
                        )
                    expected_arguments = configured.get("arguments")
                    if arguments != expected_arguments:
                        raise RuntimeContractError(
                            f"Prompt agent used unexpected arguments for '{name}'."
                        )
                    result_output = json.dumps(
                        configured.get("result"),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result_output,
                    }
                )
                called_tools.append(name)
            response_id = str(response.get("id") or "")
            if not response_id:
                raise RuntimeContractError("Prompt response omitted its response ID.")
            self._require_not_cancelled(cancelled)
            raw_response = self._invoke_response(
                receipt,
                fixture,
                "/openai/v1/responses",
                {
                    "input": outputs,
                    "previous_response_id": response_id,
                    "store": True,
                    "agent_reference": reference,
                },
                cancelled=cancelled,
            )
            response = raw_response.json()
            response_id = str(response.get("id") or "")
            if not response_id or response_id in response_ids:
                raise RuntimeContractError(
                    "Prompt response IDs must be non-empty and unique across turns."
                )
            response_ids.append(response_id)
        raise RuntimeContractError("Prompt agent exceeded the bounded tool turn limit.")

    def invoke_hosted(
        self,
        receipt: DeploymentReceipt,
        fixture: HealthyFixture,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> InvocationReceipt:
        if receipt.agent_type not in {"hosted_code", "hosted_custom_container"}:
            raise RuntimeContractError("Hosted invocation requires a hosted deployment.")
        self._require_not_cancelled(cancelled)
        session_response = self._invoke_response(
            receipt,
            fixture,
            f"/agents/{_quote(receipt.agent_name)}/endpoint/sessions",
            {
                "version_indicator": {
                    "type": "version_ref",
                    "agent_version": receipt.agent_version,
                }
            },
            hosted=True,
            cancelled=cancelled,
        )
        session = session_response.json()
        session_id = _first_text(session, "agent_session_id", "session_id", "id")
        indicator = session.get("version_indicator")
        resolved = (
            str(indicator.get("agent_version") or "")
            if isinstance(indicator, Mapping)
            else ""
        )
        indicator_type = (
            str(indicator.get("type") or "") if isinstance(indicator, Mapping) else ""
        )
        if not session_id:
            raise RuntimeContractError(
                "Hosted session did not bind to the exact deployed version."
            )
        try:
            if indicator_type != "version_ref" or resolved != receipt.agent_version:
                raise RuntimeContractError(
                    "Hosted session did not bind to the exact deployed version."
                )
            self._require_not_cancelled(cancelled)
            raw_response = self._invoke_response(
                receipt,
                fixture,
                (
                    f"/agents/{_quote(receipt.agent_name)}"
                    "/endpoint/protocols/openai/responses"
                ),
                {
                    "input": fixture.input,
                    "store": False,
                    "agent_session_id": session_id,
                },
                hosted=True,
                cancelled=cancelled,
                session_id=session_id,
            )
            response = raw_response.json()
            return _invocation_receipt(
                receipt,
                fixture,
                response,
                _invocation_id(response),
                _request_id(raw_response),
                session_id,
                (),
                (str(response.get("id") or ""),),
                validate_tools=False,
            )
        finally:
            self._delete_session(receipt.agent_name, session_id)

    def _require_not_cancelled(
        self,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        if cancelled is not None and cancelled():
            raise RuntimeContractError("Endpoint invocation was cancelled.")

    def _interruptible_sleep(
        self,
        seconds: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        remaining = seconds
        while remaining > 0:
            self._require_not_cancelled(cancelled)
            interval = min(0.1, remaining)
            self._sleep(interval)
            remaining -= interval

    def _delete_session(self, agent_name: str, session_id: str) -> None:
        self._request(
            "DELETE",
            f"/agents/{_quote(agent_name)}/endpoint/sessions/{_quote(session_id)}",
            body=None,
            hosted=True,
            expected_statuses={200, 202, 204, 404},
        )

    def _post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        hosted: bool = False,
    ) -> Mapping[str, Any]:
        return self._post_response(path, body, hosted=hosted).json()

    def _post_response(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        hosted: bool = False,
    ) -> HttpResponse:
        return self._request(
            "POST",
            path,
            body=_json_bytes(body),
            hosted=hosted,
            include_api_version=not path.startswith("/openai/v1/"),
        )

    @staticmethod
    def _retry_after(response: HttpResponse) -> float | None:
        for name, value in response.headers.items():
            if name.casefold() != "retry-after":
                continue
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                return None
            return seconds if 0 <= seconds <= 300 else None
        return None

    @staticmethod
    def _response_identity(
        response: HttpResponse,
    ) -> tuple[str | None, str | None]:
        try:
            payload = response.json()
        except RuntimeContractError:
            return None, None
        response_id = str(payload.get("id") or "") or None
        return response_id, _invocation_id(payload)

    def _endpoint_error(
        self,
        receipt: DeploymentReceipt,
        response: HttpResponse,
        *,
        session_id: str | None,
    ) -> InvocationEndpointError:
        response_id, invocation_id = self._response_identity(response)
        status = response.status_code
        transient = response_id is None and (
            status in {408, 429} or 500 <= status <= 599
        )
        code = (
            "endpoint_rate_limited"
            if status == 429
            else "endpoint_request_timeout"
            if status == 408
            else "endpoint_service_unavailable"
            if 500 <= status <= 599
            else "endpoint_invocation_failed"
        )
        return InvocationEndpointError(
            f"Agent endpoint returned HTTP {status}.",
            InvocationFailureReceipt(
                agent_name=receipt.agent_name,
                agent_version=receipt.agent_version,
                http_status=status,
                response_id=response_id,
                invocation_id=invocation_id,
                request_id=_request_id(response),
                session_id=session_id,
            ),
            code=code,
            transient=transient,
            retry_after_seconds=self._retry_after(response),
        )

    def _invoke_response(
        self,
        receipt: DeploymentReceipt,
        fixture: HealthyFixture,
        path: str,
        body: Mapping[str, Any],
        *,
        hosted: bool = False,
        cancelled: Callable[[], bool] | None = None,
        session_id: str | None = None,
    ) -> HttpResponse:
        del fixture
        for attempt in range(1, self._transient_retry_attempts + 1):
            self._require_not_cancelled(cancelled)
            response = self._call(
                "POST",
                path,
                body=_json_bytes(body),
                hosted=hosted,
                include_api_version=not path.startswith("/openai/v1/"),
            )
            if response.status_code in {200, 201, 202}:
                return response
            error = self._endpoint_error(
                receipt,
                response,
                session_id=session_id,
            )
            if not error.transient or attempt == self._transient_retry_attempts:
                raise error
            delay = (
                error.retry_after_seconds
                if error.retry_after_seconds is not None
                else min(
                    self._transient_retry_interval * (2 ** (attempt - 1)),
                    300,
                )
            )
            self._interruptible_sleep(delay, cancelled)
        raise AssertionError("unreachable")

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        hosted: bool,
        include_api_version: bool = True,
    ) -> HttpResponse:
        """Send the request and return the raw response without status checking."""
        token = self._token_provider().strip()
        if not token or token == "******":
            raise RuntimeContractError("Token provider returned no usable access token.")
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if hosted:
            headers["Foundry-Features"] = HOSTED_FEATURES
        separator = "&" if "?" in path else "?"
        request_path = (
            f"{path}{separator}api-version={API_VERSION}" if include_api_version else path
        )
        return self._transport.request(
            method,
            f"{self._endpoint}{request_path}",
            headers=headers,
            body=body,
            timeout_seconds=self._request_timeout,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        hosted: bool,
        expected_statuses: set[int] | None = None,
        include_api_version: bool = True,
    ) -> HttpResponse:
        response = self._call(
            method, path, body=body, hosted=hosted, include_api_version=include_api_version
        )
        allowed = expected_statuses or {200, 201, 202}
        if response.status_code not in allowed:
            raise RuntimeContractError(
                f"Agent endpoint request failed with HTTP {response.status_code}."
            )
        return response

def run_healthy_traffic(
    client: FoundryInvocationClient,
    receipt: DeploymentReceipt,
    fixtures: Sequence[HealthyFixture],
    *,
    max_workers: int,
) -> tuple[InvocationReceipt, ...]:
    if max_workers < 1 or max_workers > 8:
        raise RuntimeContractError("Healthy traffic concurrency must be between 1 and 8.")
    from concurrent.futures import ThreadPoolExecutor, as_completed

    invoke = client.invoke_prompt if receipt.agent_type == "prompt" else client.invoke_hosted
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_by_id = {
            executor.submit(invoke, receipt, fixture): fixture.id for fixture in fixtures
        }
        completed = {future_by_id[future]: future.result() for future in as_completed(future_by_id)}
    if set(completed) != {fixture.id for fixture in fixtures}:
        raise RuntimeContractError("Healthy traffic did not complete every fixture.")
    return tuple(completed[fixture.id] for fixture in fixtures)


def _invocation_receipt(
    deployment: DeploymentReceipt,
    fixture: HealthyFixture,
    response: Mapping[str, Any],
    invocation_id: str | None,
    request_id: str | None,
    session_id: str | None,
    called_tools: tuple[str, ...],
    response_ids: tuple[str, ...],
    *,
    validate_tools: bool,
) -> InvocationReceipt:
    status = str(response.get("status") or "").casefold()
    response_id = str(response.get("id") or "")
    output_text = _response_text(response)
    expected_status = fixture.expected_final_status.casefold()
    if status != expected_status or not response_id:
        raise RuntimeContractError(
            f"Agent response status '{status}' did not match expected '{expected_status}' "
            "or omitted a response ID."
        )
    if (
        not response_ids
        or response_ids[-1] != response_id
        or len(response_ids) != len(set(response_ids))
        or any(not value for value in response_ids)
    ):
        raise RuntimeContractError(
            "Invocation response ID history must be ordered, unique, and end with the final ID."
        )
    if fixture.validate_output and (
        not output_text.strip() or fixture.output_contains not in output_text
    ):
        raise RuntimeContractError(
            f"Healthy fixture '{fixture.id}' returned an unexpected outcome."
        )
    if validate_tools and called_tools != fixture.expected_tool_calls:
        raise RuntimeContractError(
            f"Healthy fixture '{fixture.id}' used an unexpected tool sequence."
        )
    return InvocationReceipt(
        fixture_id=fixture.id,
        agent_name=deployment.agent_name,
        agent_version=deployment.agent_version,
        response_id=response_id,
        invocation_id=invocation_id,
        request_id=request_id,
        session_id=session_id,
        output_text=output_text,
        called_tools=called_tools,
        response_ids=response_ids,
    )


def _response_text(response: Mapping[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str):
        return direct
    parts = []
    for item in response.get("output", []):
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content", []):
            if isinstance(content, Mapping) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(parts)


def _version_from_response(response: HttpResponse) -> str:
    return _version_from_mapping(response.json())


def _version_from_mapping(response: Mapping[str, Any]) -> str:
    version = str(response.get("version") or "")
    if not version:
        versions = response.get("versions")
        latest = versions.get("latest") if isinstance(versions, Mapping) else None
        if isinstance(latest, Mapping):
            version = str(latest.get("version") or "")
    if not version:
        raise RuntimeContractError("Foundry create response omitted the agent version.")
    return version


def _ownership_metadata(
    run_id: str,
    artifact_digest: str,
    *,
    source_digest: str | None = None,
    image_digest: str | None = None,
) -> dict[str, str]:
    if not run_id or len(run_id) > 64:
        raise RuntimeContractError("Run ID must be non-empty and at most 64 characters.")
    if not _DIGEST.fullmatch(artifact_digest):
        raise RuntimeContractError("Artifact digest must be sha256-prefixed lowercase hex.")
    metadata = {
        OWNER_KEY: OWNER_VALUE,
        RUN_KEY: run_id,
        DIGEST_KEY: artifact_digest,
    }
    for key, digest in (
        (SOURCE_DIGEST_KEY, source_digest),
        (IMAGE_DIGEST_KEY, image_digest),
    ):
        if digest is not None:
            if not _DIGEST.fullmatch(digest):
                raise RuntimeContractError(f"{key} must be sha256-prefixed lowercase hex.")
            metadata[key] = digest
    return metadata


def _invocation_id(
    body: Mapping[str, Any],
) -> str | None:
    value = body.get("invocation_id")
    return str(value) if value else None


def _request_id(response: HttpResponse) -> str | None:
    for key, header_value in response.headers.items():
        if key.casefold() in {"x-ms-request-id", "x-request-id"} and header_value:
            return str(header_value)
    return None


def _first_text(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if item:
            return str(item).strip()
    return ""


def _validate_project_endpoint(endpoint: str) -> str:
    value = endpoint.rstrip("/")
    parsed = urllib.parse.urlparse(value)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".services.ai.azure.com")
        or hostname in FORBIDDEN_INGESTION_HOSTS
        or "/api/projects/" not in parsed.path
    ):
        raise RuntimeContractError(
            "Foundry project endpoint must be an HTTPS services.ai.azure.com project URL."
        )
    return value


def _validate_agent_name(name: str) -> None:
    if len(name) > 63 or not _AGENT_NAME.fullmatch(name):
        raise RuntimeContractError("Agent name must preserve an exact stable aiq-NNN prefix.")


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")
