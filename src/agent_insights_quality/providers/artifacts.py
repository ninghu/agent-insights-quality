from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

import yaml

from agent_insights_quality.contracts import Environment, JsonObject, Target
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.container_environment import container_environment
from agent_insights_quality.providers.hosted import hosted_definition
from agent_insights_quality.providers.transport import HOSTED_FEATURES, encode

DEPLOYMENT_API_VERSION = "v1"


@dataclass(frozen=True)
class Artifact:
    definition: JsonObject
    archive: bytes | None = None
    context_hash: str | None = None
    context: Mapping[str, bytes] | None = field(default=None, repr=False)


def deployment_content_hash(
    target: Target, environment: Environment, artifact: Artifact,
) -> str:
    """Hash resolved deployment inputs, never commit history or evaluation assets."""
    document = {
        "schema": "aiq-deployment-content-v1",
        "target_key": target.key,
        "agent_name": target.runtime_name(environment.profile),
        "agent_type": target.agent_type,
        "profile": environment.profile,
        "project_endpoint": environment.project_endpoint,
        "api_version": DEPLOYMENT_API_VERSION,
        "features": None if target.is_prompt else HOSTED_FEATURES,
        "definition": artifact.definition,
        "archive_sha256": (
            hashlib.sha256(artifact.archive).hexdigest()
            if artifact.archive is not None else None
        ),
        "context_sha256": artifact.context_hash,
    }
    try:
        body = json.dumps(
            document, sort_keys=True, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise QualityError("deployment_content_invalid", request_accepted=False) from None
    return "v1:sha256:" + hashlib.sha256(body).hexdigest()


class ImageBuilder(Protocol):
    async def ensure_image(
        self, target: Target, source_revision: str, context: Mapping[str, bytes]
    ) -> str: ...


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise QualityError("deployment_source_missing")
    try:
        return path.read_bytes()
    except OSError:
        raise QualityError("deployment_source_unreadable") from None


def _yaml(path: Path) -> JsonObject:
    try:
        value = yaml.safe_load(_read(path))
    except (ValueError, yaml.YAMLError):
        raise QualityError("deployment_definition_invalid") from None
    if not isinstance(value, dict):
        raise QualityError("deployment_definition_invalid")
    return value


def source_files(target: Target, *, container: bool = False) -> dict[str, bytes]:
    root = target.version_root / "source"
    if root.is_symlink() or not root.is_dir():
        raise QualityError("deployment_source_missing")
    prefix = "v0/" if container else ""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise QualityError("deployment_source_symlink")
        if any(
            part in {"__pycache__", ".git"} for part in path.relative_to(root).parts
        ):
            continue
        if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            files[prefix + "source/" + path.relative_to(root).as_posix()] = _read(path)
    if not files:
        raise QualityError("deployment_source_empty")
    for filename in (
        ("requirements.txt", "Dockerfile")
        if container
        else ("requirements.txt", "host.yaml")
    ):
        files[prefix + filename] = _read(target.baseline_root / filename)
    return files


def source_zip(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, body)
    return output.getvalue()


def multipart(
    definition: JsonObject, metadata: JsonObject, archive: bytes
) -> tuple[bytes, str, str]:
    checksum = hashlib.sha256(archive).hexdigest()
    boundary = "aiq-" + checksum
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
        "Content-Type: application/json\r\n\r\n"
    ).encode() + encode({"definition": definition, "metadata": metadata})
    body += (
        (
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="code"; '
            'filename="agent.zip"\r\nContent-Type: application/zip\r\n\r\n'
        ).encode()
        + archive
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}", checksum


def prepare_artifact_inputs(
    target: Target,
    environment: Environment,
    *,
    hosted_environment: Mapping[str, str],
) -> Artifact:
    if target.is_prompt:
        try:
            asset = json.loads(_read(target.version_root / "definition.json"))
        except (ValueError, UnicodeError):
            raise QualityError("deployment_definition_invalid") from None
        definition = asset.get("definition") if isinstance(asset, dict) else None
        if not isinstance(definition, dict) or definition.get("kind") != "prompt":
            raise QualityError("deployment_definition_invalid")
        if definition.get("tools") or definition.get("tool_resources"):
            raise QualityError("prompt_tools_forbidden")
        return Artifact(definition)
    definition = hosted_definition(
        environment,
        container_environment(hosted_environment)
        if target.agent_type == "hosted_custom_container" else hosted_environment,
    )
    if target.agent_type == "hosted_code":
        host = _yaml(target.baseline_root / "host.yaml")
        entrypoint = host.get("entrypoint")
        if not isinstance(entrypoint, str) or not entrypoint.strip():
            raise QualityError("deployment_entrypoint_missing")
        definition["code_configuration"] = {
            "runtime": "python_3_13",
            "entry_point": shlex.split(entrypoint),
            "dependency_resolution": "remote_build",
        }
        return Artifact(definition, source_zip(source_files(target)))
    if target.agent_type != "hosted_custom_container":
        raise QualityError("deployment_agent_type_unsupported")
    context = source_files(target, container=True)
    return Artifact(
        definition, context_hash=hashlib.sha256(source_zip(context)).hexdigest(), context=context,
    )


async def build_artifact(
    target: Target, source_revision: str, artifact: Artifact, *, images: ImageBuilder | None,
) -> Artifact:
    if target.agent_type != "hosted_custom_container":
        return artifact
    if images is None:
        raise QualityError("container_builder_unavailable", request_accepted=False)
    if artifact.context is None:
        raise QualityError("container_context_missing", request_accepted=False)
    image = await images.ensure_image(target, source_revision, artifact.context)
    if not isinstance(image, str) or not re.fullmatch(
        r"[a-z0-9.-]+/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", image
    ):
        raise QualityError("container_image_not_pinned")
    return replace(
        artifact, context=None,
        definition={**artifact.definition, "container_configuration": {"image": image}},
    )


async def prepare_artifact(
    target: Target,
    environment: Environment,
    source_revision: str,
    *,
    images: ImageBuilder | None,
    hosted_environment: Mapping[str, str],
) -> Artifact:
    inputs = prepare_artifact_inputs(target, environment, hosted_environment=hosted_environment)
    return await build_artifact(target, source_revision, inputs, images=images)
