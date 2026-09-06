from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.contracts import JsonObject, Target
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.artifacts import source_zip
from agent_insights_quality.providers.callbacks import safe_persist
from agent_insights_quality.providers.cancellation import drain_on_cancel


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = field(default="", repr=False)


class CommandRunner(Protocol):
    async def run(
        self, arguments: Sequence[str], *, cwd: Path | None = None
    ) -> CommandResult: ...


class AzureCommandRunner:
    async def run(
        self, arguments: Sequence[str], *, cwd: Path | None = None
    ) -> CommandResult:
        return await drain_on_cancel(asyncio.to_thread(self._run, arguments, cwd))

    def _run(self, arguments: Sequence[str], cwd: Path | None) -> CommandResult:
        try:
            result = subprocess.run(
                [azure_cli(), *arguments],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise QualityError(
                "acr_command_no_response", request_accepted=None
            ) from None
        return CommandResult(result.returncode, result.stdout)


class AcrImageBuilder:
    """Source-built images with retained private contexts, native runs, and OCI pinning."""

    def __init__(
        self,
        registry_name: str,
        *,
        workspace: Path,
        persist: Callable[[JsonObject], None],
        records: Mapping[str, JsonObject] | None = None,
        command: CommandRunner | None = None,
        repository: str = "agent-insights-quality-support",
    ) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9]{5,50}", registry_name) or not re.fullmatch(
            r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", repository
        ):
            raise QualityError("acr_configuration_invalid")
        self.registry_name = registry_name
        self.repository = repository
        self.workspace = workspace
        self.persist = safe_persist(persist)
        self.records = copy.deepcopy(dict(records or {}))
        self.command = command if command is not None else AzureCommandRunner()

    def _save(self, value: JsonObject) -> None:
        self.persist(copy.deepcopy(value))
        self.records[value["artifact_key"]] = copy.deepcopy(value)

    async def _read(self, *arguments: str) -> object:
        result = await self.command.run(
            [
                "acr",
                *arguments,
                "--name" if arguments[0] == "repository" else "--registry",
                self.registry_name,
                "--only-show-errors",
                "--output",
                "json",
            ]
        )
        if result.returncode:
            raise QualityError("acr_read_failed", request_accepted=False)
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise QualityError("acr_response_invalid") from None

    def _image(self, digest: object) -> str:
        if not isinstance(digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ):
            raise QualityError("acr_digest_missing")
        return f"{self.registry_name}.azurecr.io/{self.repository}@{digest}"

    async def _lookup(self, tag: str) -> str | None:
        repositories = await self._read("repository", "list")
        if not isinstance(repositories, list) or any(
            not isinstance(item, str) for item in repositories
        ):
            raise QualityError("acr_response_invalid")
        if self.repository not in repositories:
            return None
        tags = await self._read(
            "repository", "show-tags", "--repository", self.repository, "--detail"
        )
        if not isinstance(tags, list) or any(
            not isinstance(item, dict) for item in tags
        ):
            raise QualityError("acr_response_invalid")
        matches = [item for item in tags if item.get("name") == tag]
        if len(matches) > 1:
            raise QualityError("acr_tag_ambiguous")
        return self._image(matches[0].get("digest")) if matches else None

    async def ensure_image(
        self, target: Target, source_revision: str, context: Mapping[str, bytes]
    ) -> str:
        key = hashlib.sha256(source_zip(context)).hexdigest()
        tag = "source-" + key
        known = self.records.get(key)
        image = await self._lookup(tag)
        if image is not None:
            self._save(
                {
                    **(known or {}),
                    "artifact_key": key,
                    "image": image,
                    "state": "completed",
                    "source_revision": source_revision,
                    "target_key": target.key,
                }
            )
            return image
        if known and known.get("run_id"):
            return await self._poll(known, tag)
        if known:
            raise QualityError("acr_build_unresolved", request_accepted=None)
        record = {
            "artifact_key": key,
            "state": "submitting",
            "source_revision": source_revision,
            "target_key": target.key,
            "tag": tag,
        }
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(
                prefix="acr-source-", dir=self.workspace
            ))
        except OSError:
            raise QualityError(
                "acr_workspace_unavailable", request_accepted=False
            ) from None
        try:
            for name, body in context.items():
                path = root / name
                if (
                    path.resolve() == root.resolve()
                    or not path.resolve().is_relative_to(root.resolve())
                ):
                    raise QualityError(
                        "acr_context_path_invalid", request_accepted=False
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
        except OSError:
            raise QualityError(
                "acr_context_unavailable", request_accepted=False
            ) from None
        record["context_path"] = str(root)
        self._save(record)
        saved = await drain_on_cancel(self._submit(record, root))
        return await self._poll(saved, tag)

    async def _submit(self, record: JsonObject, root: Path) -> JsonObject:
        try:
            # The CLI wrapper suppresses output with --no-wait. --no-logs waits
            # for the run and returns its structured result, never console logs.
            result = await self.command.run(
                [
                    "acr", "build", "--registry", self.registry_name,
                    "--image", self.repository + ":" + record["tag"],
                    "--file", "v0/Dockerfile", "--no-logs",
                    "--only-show-errors", "--output", "json", ".",
                ],
                cwd=root,
            )
        except (QualityError, OSError):
            self._save({**record, "state": "unknown"})
            raise QualityError("acr_build_unknown", request_accepted=None) from None
        return self._submitted(record, result)

    def _submitted(self, record: JsonObject, result: CommandResult) -> JsonObject:
        if result.returncode:
            self._save({**record, "state": "unknown"})
            raise QualityError("acr_build_unknown", request_accepted=None)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            self._save({**record, "state": "unknown"})
            raise QualityError(
                "acr_build_identity_missing", request_accepted=None
            ) from None
        properties = self._run_properties(value)
        run_id = properties.get("runId")
        if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id) is None:
            self._save({**record, "state": "unknown"})
            raise QualityError("acr_build_identity_missing", request_accepted=None)
        saved = {**record, "state": "pending", "run_id": run_id}
        self._save(saved)
        return saved

    async def _poll(self, record: JsonObject, tag: str) -> str:
        value = await self._read("task", "show-run", "--run-id", record["run_id"])
        properties = self._run_properties(value)
        if properties.get("runId") != record["run_id"]:
            raise QualityError("acr_run_identity_mismatch", request_accepted=True)
        status = properties.get("status")
        if not isinstance(status, str):
            raise QualityError("acr_response_invalid")
        if status in {"Failed", "Canceled", "Error", "Timeout"}:
            self._save({**record, "state": "failed", "provider_response": value})
            raise QualityError("acr_build_failed", request_accepted=True)
        if status != "Succeeded":
            raise QualityError(
                "acr_build_pending", request_accepted=True, retryable=True
            )
        image = await self._lookup(tag)
        if image is None:
            raise QualityError(
                "acr_image_pending", request_accepted=True, retryable=True
            )
        self._save(
            {**record, "state": "completed", "image": image, "provider_response": value}
        )
        return image

    @staticmethod
    def _run_properties(value: object) -> JsonObject:
        if not isinstance(value, dict):
            return {}
        # Current ARM-backed CLI models nest run fields under properties;
        # older CLI serializers flatten the same native fields.
        properties = value.get("properties", value)
        return properties if isinstance(properties, dict) else {}
