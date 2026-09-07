from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from agent_insights_quality.contracts import Deployment, Environment, JsonObject, Target
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.artifacts import (
    DEPLOYMENT_API_VERSION,
    ImageBuilder,
    build_artifact,
    deployment_content_hash,
    multipart,
    prepare_artifact_inputs,
)
from agent_insights_quality.providers.callbacks import safe_persist
from agent_insights_quality.providers.transport import HttpResponse, JsonClient, check_status, segment


def _version(value: Mapping[str, Any], *, created: bool = False) -> str:
    version = value.get("version")
    if created and not version:
        # This is the identity returned by our create, never a traffic-time "latest" lookup.
        versions = value.get("versions")
        latest = versions.get("latest") if isinstance(versions, dict) else None
        version = latest.get("version") if isinstance(latest, dict) else None
    return (
        str(version)
        if isinstance(version, (str, int)) and not isinstance(version, bool)
        else ""
    )


def _state(value: Mapping[str, Any], *, prompt: bool) -> str:
    status = str(value.get("status") or "").lower()
    if status in {"failed", "canceled", "cancelled", "deleted"}:
        return "failed"
    if status == "active" or (prompt and not status):
        return "active"
    return "pending"


def _private_response(response: HttpResponse) -> JsonObject:
    try:
        return response.object()
    except QualityError:
        return {"raw_body": response.body.decode("utf-8", errors="replace")}


class DeploymentClient:
    def __init__(
        self,
        environment: Environment,
        client: JsonClient,
        *,
        images: ImageBuilder | None = None,
        hosted_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.environment = environment
        self.client = client
        self.images = images
        self.hosted_environment = dict(hosted_environment or {})

    async def ensure_deployment(
        self,
        target: Target,
        source_revision: str,
        existing: Deployment | None,
        persist: Callable[[Deployment], None],
        *,
        resume: bool = False,
    ) -> Deployment:
        persist = safe_persist(persist)
        name = target.runtime_name(self.environment.profile)
        if not source_revision:
            raise QualityError(
                "deployment_source_revision_missing", request_accepted=False
            )
        if existing and (
            existing.target_key != target.key
            or existing.agent_name != name
            or existing.agent_type != target.agent_type
        ):
            raise QualityError("deployment_identity_mismatch", request_accepted=False)
        if resume and (existing is None or existing.source_revision != source_revision):
            raise QualityError("deployment_identity_mismatch", request_accepted=False)
        unresolved = {"submitting", "unknown", "pending", "failed"}
        if not resume and existing and existing.details.get("provisioning_state") in unresolved:
            raise QualityError("deployment_create_unresolved", request_accepted=None)
        artifact = None
        if resume:
            content_hash = existing.content_hash
        else:
            inputs = prepare_artifact_inputs(
                target, self.environment, hosted_environment=self.hosted_environment,
            )
            artifact = await build_artifact(target, source_revision, inputs, images=self.images)
            content_hash = deployment_content_hash(target, self.environment, artifact)
        # A work checkpoint pins the original submission, including legacy provenance.
        # A registry entry is only a reuse candidate, never an instruction to resume work.
        same = resume or existing is not None and (
            content_hash is not None and existing.content_hash == content_hash
        )
        # Discover every content owner before enforcing frozen provenance: filtering
        # by commit first could let a rejected retry duplicate another owner's version.
        metadata = self._metadata(
            target, source_revision if resume and content_hash is None else None, content_hash,
        )
        path = f"/agents/{segment(name)}"
        if same and existing.provider_version:
            response = await self.client.request(
                "GET",
                path + "/versions/" + segment(existing.provider_version),
                hosted=not target.is_prompt,
                api_version=DEPLOYMENT_API_VERSION,
            )
            check_status(response, {200, 404}, read_only=True)
            if response.status == 200:
                return self._observed(
                    existing, response.object(), target, persist,
                    self._metadata(target, existing.source_revision, content_hash),
                )
            if existing.details.get("provisioning_state") != "active":
                raise QualityError(
                    "deployment_propagation_pending",
                    retryable=True,
                    request_accepted=True,
                )
            if resume:
                raise QualityError("deployment_frozen_version_missing", request_accepted=True)

        response = await self.client.request(
            "GET", path, hosted=not target.is_prompt, api_version=DEPLOYMENT_API_VERSION,
        )
        check_status(response, {200, 404}, read_only=True)
        create_agent = response.status == 404
        matches = []
        if not create_agent:
            versions = await self.client.pages(
                self.client.url(path + "/versions?limit=100", api_version=DEPLOYMENT_API_VERSION),
                hosted=not target.is_prompt,
            )
            matches = [
                item
                for item in versions
                if isinstance(item.get("metadata"), dict)
                and all(
                    item["metadata"].get(key) == value
                    for key, value in metadata.items()
                )
            ]
        if len(matches) > 1:
            raise QualityError("deployment_versions_ambiguous", request_accepted=None)
        if matches:
            version = _version(matches[0])
            if not version:
                raise QualityError("deployment_version_missing", request_accepted=True)
            provenance = matches[0]["metadata"].get("aiq_source_revision")
            if not isinstance(provenance, str) or not provenance:
                raise QualityError("deployment_provenance_missing", request_accepted=True)
            if resume and provenance != source_revision:
                raise QualityError("deployment_provenance_conflict", request_accepted=None)
            observed_metadata = self._metadata(target, provenance, content_hash)
            recovered = Deployment(
                target.key, name, version, target.agent_type, provenance,
                {"metadata": observed_metadata}, content_hash,
            )
            return self._observed(
                recovered, matches[0], target, persist, observed_metadata,
            )
        if same and existing.details.get("provisioning_state") in unresolved:
            raise QualityError(
                "deployment_create_unresolved",
                retryable=existing.details.get("provisioning_state") == "pending",
                request_accepted=None,
            )
        if resume:
            if existing.details.get("provisioning_state") != "rejected":
                raise QualityError("deployment_create_unresolved", request_accepted=None)
            if content_hash is None:
                raise QualityError(
                    "deployment_legacy_retry_requires_new_work", request_accepted=False,
                )
            inputs = prepare_artifact_inputs(
                target, self.environment, hosted_environment=self.hosted_environment,
            )
            if deployment_content_hash(target, self.environment, inputs) != existing.details.get("inputs_hash"):
                raise QualityError("deployment_content_changed", request_accepted=False)
            artifact = await build_artifact(target, source_revision, inputs, images=self.images)
            if deployment_content_hash(target, self.environment, artifact) != content_hash:
                raise QualityError("deployment_content_changed", request_accepted=False)
        metadata = self._metadata(target, source_revision, content_hash)
        route = "/agents" if create_agent else path + "/versions"
        pending = Deployment(
            target.key,
            name,
            "",
            target.agent_type,
            source_revision,
            {
                "provisioning_state": "submitting", "metadata": metadata,
                "inputs_hash": deployment_content_hash(target, self.environment, inputs),
            },
            content_hash,
        )
        persist(pending)
        response = None
        try:
            if artifact.archive is not None:
                body, content_type, checksum = multipart(
                    artifact.definition, metadata, artifact.archive
                )
                response = await self.client.request(
                    "POST",
                    route,
                    hosted=True,
                    api_version=DEPLOYMENT_API_VERSION,
                    body=body,
                    headers={
                        "Content-Type": content_type,
                        "x-ms-agent-name": name,
                        "x-ms-code-zip-sha256": checksum,
                    },
                )
            else:
                payload = {"definition": artifact.definition, "metadata": metadata}
                if create_agent:
                    payload["name"] = name
                response = await self.client.request(
                    "POST", route, payload, hosted=not target.is_prompt,
                    api_version=DEPLOYMENT_API_VERSION,
                )
            check_status(response, {200, 201, 202})
        except QualityError as error:
            persist(
                replace(
                    pending,
                    details={
                        **pending.details,
                        "provisioning_state": "rejected"
                        if error.request_accepted is False
                        else "unknown",
                        "error_code": error.code,
                        "http_status": error.status,
                        **({"provider_response": _private_response(response)}
                           if response is not None else {}),
                    },
                )
            )
            raise
        try:
            value = response.object()
        except QualityError:
            persist(
                replace(
                    pending,
                    details={
                        **pending.details,
                        "provisioning_state": "unknown",
                        "http_status": response.status,
                        "provider_response": _private_response(response),
                    },
                )
            )
            raise
        version = _version(value, created=True)
        saved = replace(
            pending,
            provider_version=version,
            details={
                **pending.details,
                "provisioning_state": "pending",
                "provider_response": value,
                "http_status": response.status,
                "polling_url": response.header("Operation-Location")
                or response.header("Location"),
            },
        )
        persist(saved)
        if not version:
            raise QualityError(
                "deployment_create_identity_pending",
                request_accepted=True,
                retryable=True,
            )
        if response.status == 202:
            raise QualityError(
                "deployment_pending", request_accepted=True, retryable=True
            )
        return self._observed(saved, value, target, persist, metadata, created=True)

    def _metadata(
        self, target: Target, source_revision: str | None, content_hash: str | None,
    ) -> dict[str, str]:
        metadata = {
            "aiq_profile": self.environment.profile,
            "aiq_logical_version": target.unit_id.logical_version,
        }
        if source_revision is not None:
            metadata["aiq_source_revision"] = source_revision
        if content_hash is not None:
            metadata.update(aiq_content_hash=content_hash, aiq_agent_type=target.agent_type)
        return metadata

    def _observed(
        self,
        deployment: Deployment,
        value: JsonObject,
        target: Target,
        persist: Callable[[Deployment], None],
        metadata: Mapping[str, str],
        *,
        created: bool = False,
    ) -> Deployment:
        observed_version = _version(value, created=created)
        if not observed_version:
            raise QualityError("deployment_version_missing", request_accepted=True)
        if observed_version != deployment.provider_version:
            raise QualityError("deployment_version_mismatch", request_accepted=True)
        remote_metadata = value.get("metadata")
        if deployment.content_hash is not None and not created and (
            not isinstance(remote_metadata, dict)
            or any(remote_metadata.get(key) != expected for key, expected in metadata.items())
        ):
            raise QualityError("deployment_metadata_mismatch", request_accepted=True)
        if isinstance(remote_metadata, dict) and any(
            key in remote_metadata and remote_metadata[key] != expected
            for key, expected in metadata.items()
        ):
            raise QualityError("deployment_metadata_mismatch", request_accepted=True)
        state = _state(value, prompt=target.is_prompt)
        saved = replace(
            deployment,
            details={
                **deployment.details,
                "provisioning_state": state,
                "provider_response": value,
            },
        )
        persist(saved)
        if state != "active":
            raise QualityError(
                "deployment_failed" if state == "failed" else "deployment_pending",
                retryable=state == "pending",
                request_accepted=True,
            )
        return saved

    async def activate(self, deployment: Deployment) -> None:
        if not deployment.provider_version:
            raise QualityError("deployment_version_missing", request_accepted=False)
        if deployment.agent_type == "prompt":
            return
        desired = [
            {
                "agent_version": deployment.provider_version,
                "traffic_percentage": 100,
                "type": "FixedRatio",
            }
        ]
        value = await self.client.object(
            "PATCH",
            "/agents/" + segment(deployment.agent_name),
            {
                "agent_endpoint": {
                    "version_selector": {"version_selection_rules": desired}
                }
            },
            hosted=True,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        endpoint = value.get("agent_endpoint")
        selector = (
            endpoint.get("version_selector") if isinstance(endpoint, dict) else None
        )
        rules = (
            selector.get("version_selection_rules")
            if isinstance(selector, dict)
            else None
        )
        if rules != desired:
            raise QualityError("deployment_route_unconfirmed", request_accepted=True)
