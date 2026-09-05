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
        return await asyncio.to_thread(self._run, arguments, cwd)

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
    """One source-built image at a time, with native ACR run checkpoints and OCI pinning."""

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
            temporary = tempfile.TemporaryDirectory(
                prefix="acr-source-", dir=self.workspace
            )
        except OSError:
            raise QualityError(
                "acr_workspace_unavailable", request_accepted=False
            ) from None
        with temporary as directory:
            root = Path(directory)
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
            self._save(record)
            try:
                result = await self.command.run(
                    [
                        "acr",
                        "build",
                        "--registry",
                        self.registry_name,
                        "--image",
                        self.repository + ":" + tag,
                        "--file",
                        "v0/Dockerfile",
                        "--no-wait",
                        "--no-logs",
                        "--only-show-errors",
                        "--output",
                        "json",
                        ".",
                    ],
                    cwd=root,
                )
            except (QualityError, OSError):
                self._save({**record, "state": "unknown"})
                raise QualityError("acr_build_unknown", request_accepted=None) from None
            saved = self._submitted(record, result)
        return await self._poll(saved, tag)

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
        run_id = value.get("runId") if isinstance(value, dict) else None
        if not isinstance(run_id, str) or not run_id:
            self._save({**record, "state": "unknown"})
            raise QualityError("acr_build_identity_missing", request_accepted=None)
        saved = {**record, "state": "pending", "run_id": run_id}
        self._save(saved)
        return saved

    async def _poll(self, record: JsonObject, tag: str) -> str:
        value = await self._read("task", "show-run", "--run-id", record["run_id"])
        if not isinstance(value, dict):
            raise QualityError("acr_response_invalid")
        status = value.get("status")
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
