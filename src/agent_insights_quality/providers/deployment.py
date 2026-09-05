from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from agent_insights_quality.contracts import Deployment, Environment, JsonObject, Target
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.artifacts import (
    ImageBuilder,
    multipart,
    prepare_artifact,
)
from agent_insights_quality.providers.callbacks import safe_persist
from agent_insights_quality.providers.container_environment import container_environment
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
        metadata = {
            "aiq_profile": self.environment.profile,
            "aiq_logical_version": target.unit_id.logical_version,
            "aiq_source_revision": source_revision,
        }
        same = existing is not None and existing.source_revision == source_revision
        path = f"/agents/{segment(name)}"
        if same and existing.provider_version:
            response = await self.client.request(
                "GET",
                path + "/versions/" + segment(existing.provider_version),
                hosted=not target.is_prompt,
            )
            check_status(response, {200, 404}, read_only=True)
            if response.status == 200:
                return self._observed(
                    existing, response.object(), target, persist, metadata
                )
            if existing.details.get("provisioning_state") != "active":
                raise QualityError(
                    "deployment_propagation_pending",
                    retryable=True,
                    request_accepted=True,
                )

        response = await self.client.request("GET", path, hosted=not target.is_prompt)
        check_status(response, {200, 404}, read_only=True)
        create_agent = response.status == 404
        matches = []
        if not create_agent:
            versions = await self.client.pages(
                path + "/versions?limit=100", hosted=not target.is_prompt
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
            recovered = Deployment(
                target.key, name, version, target.agent_type, source_revision
            )
            return self._observed(recovered, matches[0], target, persist, metadata)
        if same and existing.details.get("provisioning_state") in {
            "submitting",
            "unknown",
            "pending",
            "failed",
        }:
            raise QualityError(
                "deployment_create_unresolved",
                retryable=existing.details.get("provisioning_state") == "pending",
                request_accepted=None,
            )
        artifact = await prepare_artifact(
            target,
            self.environment,
            source_revision,
            images=self.images,
            hosted_environment=(
                container_environment(self.hosted_environment)
                if target.agent_type == "hosted_custom_container"
                else self.hosted_environment
            ),
        )
        route = "/agents" if create_agent else path + "/versions"
        pending = Deployment(
            target.key,
            name,
            "",
            target.agent_type,
            source_revision,
            {"provisioning_state": "submitting", "metadata": metadata},
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
                    "POST", route, payload, hosted=not target.is_prompt
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
