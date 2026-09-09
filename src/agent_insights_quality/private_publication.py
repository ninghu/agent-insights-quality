"""Immutable private reports. No Git, email, assessment or traffic side effects.

The synchronous provider is deliberately used only after measurement, under
RuntimeStore ownership. No SDK work is detached into cancellable worker threads.
Each flush has a deadline and at most one conditional PUT per immutable object.
Recovery reads exact frozen bytes; it never renders again or follows latest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Protocol

from .contracts import Environment
from .errors import QualityError
from .events import _directory_lock
from .privacy import restore_public_result
from .report_context import ReportMetadata, load_report_context
from .report_review import RetainedReviewContext
from .reporting import markdown_view, render_private_markdown
from .results import PlannedUnit, QualityResult
from .state import (
    RuntimeStore, StateError, _atomic_write, _confirm_durable, _encode,
    _inside, _open_snapshot, _parts, _unique_object,
)

CONTAINER = "quality-artifacts"
RENDERER = "private-md-html-v2"
MAX_BYTES = 2_000_000
FLUSH_SECONDS = 90
CALL_SECONDS = 10
LATEST = "reports/daily/official/latest.json"
_HASH = r"[0-9a-f]{64}"
_RUN = r"daily-\d{4}-\d{2}-\d{2}(?:-test-[1-9][0-9]*)?"
_TYPES = {
    "report.md": "text/markdown; charset=utf-8",
    "report.html": "text/html; charset=utf-8",
    "manifest.json": "application/json",
}


class PrivateReportError(QualityError):
    """Only code-owned messages cross the provider boundary."""


@dataclass(frozen=True)
class BlobSnapshot:
    data: bytes
    etag: str


class ReportBlobPort(Protocol):
    def verify_private(self, *, timeout: float) -> None: ...
    def read(self, key: str, *, timeout: float) -> BlobSnapshot | None: ...
    def write(
        self, key: str, data: bytes, *, content_type: str, etag: str | None, timeout: float,
    ) -> None: ...
    def sign_read_links(
        self, keys: list[str], *, starts_at: str, expires_at: str, timeout: float,
    ) -> dict[str, str]: ...
    def close(self) -> None: ...


def _hash(data: bytes) -> str:
    return sha256(data).hexdigest()


def _json(value: dict) -> bytes:
    return _encode(value)


def _account(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9]{3,24}", value) is None:
        raise PrivateReportError("private_report_account_invalid")
    return value


def _key(value: str) -> str:
    if value != LATEST and re.fullmatch(
        rf"reports/daily/(official|test|failure)/{_RUN}/{_HASH}/"
        r"(?:(?:agents/[a-z][a-z0-9-]{0,63}/)?report\.(?:md|html)|manifest\.json)",
        value,
    ) is None:
        raise PrivateReportError("private_report_key_invalid")
    return value


def _file_types(plan, *, legacy=False):
    files = dict(_TYPES)
    if not legacy:
        for agent in dict.fromkeys(unit.unit_id.agent for unit in plan):
            if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", agent) is None:
                raise PrivateReportError("private_report_agent_invalid")
            for extension in ("md", "html"):
                files[f"agents/{agent}/report.{extension}"] = _TYPES[f"report.{extension}"]
    return files


def _account_public_access(account: str, timeout: float) -> bool:
    from .bootstrap import RESOURCE_GROUP, azure_cli

    # Management-plane read only; never account keys, roles or account changes.
    try:
        result = subprocess.run(
            [azure_cli(), "storage", "account", "list", "--resource-group", RESOURCE_GROUP, "--query",
             f"[?name=='{_account(account)}'].allowBlobPublicAccess",
             "--output", "json", "--only-show-errors"],
            capture_output=True, timeout=timeout,
        )
        if result.returncode or len(result.stdout) > 1024:
            raise ValueError
        value = json.loads(result.stdout)
        if not isinstance(value, list) or len(value) != 1 or value[0] is not False:
            raise ValueError
        return False
    except (OSError, ValueError, subprocess.TimeoutExpired, QualityError):
        raise PrivateReportError("private_report_account_privacy_unverified") from None


class AzurePrivateReportBlob:
    """Existing private container, normal CLI auth or an injected token identity.

    An injected identity also supplies account_public_access, an ARM read proving
    the same account has allowBlobPublicAccess=false. No SDK imports at collection.
    """

    def __init__(
        self, account: str, *, credential=None, service=None,
        account_public_access=_account_public_access,
    ) -> None:
        self.account = _account(account)
        if credential is not None and not callable(getattr(credential, "get_token", None)):
            raise PrivateReportError("private_report_token_identity_required")
        self.credential, self.service = credential, service
        self.account_public_access = account_public_access
        self._own_credential = credential is None
        self.container = None
        self._private_verified = False

    def _client(self):
        if self.container is None:
            from azure.identity import AzureCliCredential
            from azure.storage.blob import BlobServiceClient

            if self.credential is None:
                self.credential = AzureCliCredential(process_timeout=CALL_SECONDS)
            if self.service is None:
                self.service = BlobServiceClient(
                    account_url=f"https://{self.account}.blob.core.windows.net",
                    credential=self.credential, retry_total=0,
                    connection_timeout=CALL_SECONDS, read_timeout=CALL_SECONDS,
                    max_single_put_size=MAX_BYTES, logging_enable=False,
                )
            if (
                getattr(self.service, "account_name", None) != self.account
                or getattr(self.service, "url", "").rstrip("/") != (
                    f"https://{self.account}.blob.core.windows.net"
                )
            ):
                raise PrivateReportError("private_report_service_scope_invalid")
            self.container = self.service.get_container_client(CONTAINER)
        return self.container

    @staticmethod
    def _options(timeout):
        if timeout <= 0:
            raise PrivateReportError("private_report_deadline")
        return {
            "timeout": max(1, int(timeout)), "connection_timeout": timeout,
            "read_timeout": timeout, "retry_total": 0, "logging_enable": False,
        }

    def verify_private(self, *, timeout: float) -> None:
        self._private_verified = False
        started = time.monotonic()
        if self.account_public_access(self.account, timeout) is not False:
            raise PrivateReportError("private_report_account_privacy_unverified")
        try:
            from azure.core.exceptions import AzureError
        except ImportError:
            raise PrivateReportError("private_report_dependency_unavailable") from None
        try:
            properties = self._client().get_container_properties(
                **self._options(timeout - (time.monotonic() - started)),
            )
            if "public_access" not in properties or properties["public_access"] is not None:
                raise PrivateReportError("private_report_container_not_private")
            self._private_verified = True
        except ImportError:
            raise PrivateReportError("private_report_dependency_unavailable") from None
        except (AzureError, OSError):
            raise PrivateReportError("private_report_container_privacy_unverified") from None

    def read(self, key: str, *, timeout: float) -> BlobSnapshot | None:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            download = self._client().get_blob_client(_key(key)).download_blob(
                offset=0, length=MAX_BYTES + 1, max_concurrency=1, **self._options(timeout),
            )
            data = download.readall()
            etag = download.properties.etag
            if len(data) > MAX_BYTES or not isinstance(etag, str) or not etag or etag == "*":
                raise PrivateReportError("private_report_blob_invalid")
            return BlobSnapshot(data, etag)
        except ResourceNotFoundError as error:
            if getattr(error.error_code, "value", error.error_code) == "BlobNotFound":
                return None
            raise PrivateReportError("private_report_resource_missing") from None
        except (AzureError, OSError):
            raise PrivateReportError("private_report_read_failed") from None

    def write(self, key, data, *, content_type, etag, timeout):
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError
        from azure.storage.blob import ContentSettings

        if not self._private_verified:
            raise PrivateReportError("private_report_container_privacy_unverified")
        if not isinstance(data, bytes) or len(data) > MAX_BYTES or (
            etag is not None and (key != LATEST or not etag or etag == "*")
        ):
            raise PrivateReportError("private_report_write_invalid")
        options = {"overwrite": etag is not None}
        if etag is not None:
            options.update(etag=etag, match_condition=MatchConditions.IfNotModified)
        try:
            self._client().get_blob_client(_key(key)).upload_blob(
                data, content_settings=ContentSettings(content_type=content_type),
                max_concurrency=1, **options, **self._options(timeout),
            )
        except (AzureError, OSError):
            # Includes 409/412: reconcile readback, not provider acceptance.
            raise PrivateReportError("private_report_write_unresolved", request_accepted=None) from None

    def close(self):
        try:
            try:
                if self.service is not None:
                    self.service.close()
            finally:
                if self._own_credential and self.credential is not None:
                    self.credential.close()
        except Exception:
            raise PrivateReportError("private_report_close_failed") from None

    def sign_read_links(self, keys, *, starts_at, expires_at, timeout):
        from .report_access import (
            CLOCK_SKEW, MAX_LIFETIME, ReportAccessError, _time, _utc, utc_now,
            validate_delegation_key, validate_read_sas,
        )
        from datetime import timedelta
        from azure.core.exceptions import AzureError, HttpResponseError
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        if not self._private_verified:
            raise ReportAccessError("report_access_container_privacy_unverified")
        if (
            not isinstance(keys, list) or not 1 <= len(keys) <= 10
            or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)
        ):
            raise ReportAccessError("report_access_blob_scope_invalid")
        start, end, now = _time(starts_at), _time(expires_at), _utc(utc_now())
        if (
            not timedelta(0) < end - start <= MAX_LIFETIME or not start <= now < end
            or now - start > CLOCK_SKEW + timedelta(seconds=FLUSH_SECONDS)
        ):
            raise ReportAccessError("report_access_lifetime_invalid")
        for key in keys:
            _key(key)
            if "/agents/" not in key or not key.endswith(("/report.html", "/report.md")):
                raise ReportAccessError("report_access_blob_scope_invalid")
        delegation = None
        try:
            delegation = self.service.get_user_delegation_key(
                key_start_time=_time(starts_at), key_expiry_time=_time(expires_at),
                **self._options(timeout),
            )
            validate_delegation_key(delegation, starts_at=starts_at, expires_at=expires_at)
            result = {}
            for key in keys:
                token = generate_blob_sas(
                    account_name=self.account, container_name=CONTAINER, blob_name=key,
                    user_delegation_key=delegation, permission=BlobSasPermissions(read=True),
                    start=_time(starts_at), expiry=_time(expires_at), protocol="https",
                )
                result[key] = validate_read_sas(
                    f"https://{self.account}.blob.core.windows.net/{CONTAINER}/{key}?{token}",
                    account=self.account, blob_key=key, starts_at=starts_at, expires_at=expires_at,
                )
            return result
        except HttpResponseError as error:
            code = "report_access_delegation_denied" if error.status_code in {401, 403} else "report_access_unavailable"
            raise ReportAccessError(code) from None
        except (AzureError, OSError, TypeError, ValueError):
            raise ReportAccessError("report_access_unavailable") from None
        finally:
            # Never persist the delegation key or expose it to the caller.
            delegation = None


def _write_local(runtime: RuntimeStore, path: Path, data: bytes) -> None:
    with runtime._write_lock:
        if not runtime._owned:
            raise StateError("state_not_owned")
        path = _inside(runtime.root, path)
        try:
            with _open_snapshot(path) as stream:
                existing = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            _atomic_write(path, data)
        else:
            if existing != data:
                raise PrivateReportError("private_report_local_conflict")
            _confirm_durable(path)


class PrivateReportOutbox:
    def __init__(
        self, runtime: RuntimeStore, run_id: str, *,
        presentation_id: str | None = None, clock=time.monotonic,
    ):
        if runtime.environment != "daily" or re.fullmatch(_RUN, run_id) is None:
            raise PrivateReportError("private_report_identity_invalid")
        if presentation_id is not None and (
            not isinstance(presentation_id, str) or re.fullmatch(_HASH, presentation_id) is None
        ):
            raise PrivateReportError("private_report_presentation_invalid")
        _parts(run_id)
        self.runtime, self.run_id, self.clock = runtime, run_id, clock
        self.presentation_id = presentation_id
        self.records, self.run = runtime.outbox("private-reports"), runtime.run(run_id)
        self.disabled = False
        self._flush_lock = _directory_lock(self.records.directory / run_id)

    @property
    def request_key(self):
        return "requests/" + self.run_id + (
            "/presentations/" + self.presentation_id if self.presentation_id is not None else ""
        )

    def _request_record(self, *, missing_ok=False):
        request = self.records.read_completed(self.request_key, missing_ok=True)
        if request is not None:
            return request, self.request_key
        if self.presentation_id is not None:
            original_key = "requests/" + self.run_id
            original = self.records.read_completed(original_key, missing_ok=True)
            if original is not None and original.get("presentation_id") == self.presentation_id:
                return original, original_key
        if not missing_ok:
            if self.presentation_id is not None:
                raise PrivateReportError("private_report_presentation_missing")
            self.records.read_completed(self.request_key)
        return None, self.request_key

    def _identity(self, result, plan, environment, source_revision, report_date, test_run):
        run = self.run.read_completed("run")
        assessment = self.run.read_completed("assessment-settings")
        from .settings import AssessmentSettings
        AssessmentSettings.from_dict(assessment)
        pointer = self.run.read("quality-result")
        if set(pointer) != {"artifact", "source_revision"} or not isinstance(pointer["artifact"], str):
            raise PrivateReportError("private_report_source_mismatch")
        retained = self.run.read_artifact(pointer["artifact"])
        metadata = ReportMetadata(report_date, environment.region_display, source_revision)
        if (
            run.get("kind") != "daily" or run.get("source_revision") != source_revision
            or run.get("report_date") != report_date or run.get("test_run") is not test_run
            or type(run.get("rerun")) is not int
            or (run["rerun"] < 1 if test_run else run["rerun"] != 0)
            or self.run_id != f"daily-{report_date}" + (f"-test-{run['rerun']}" if test_run else "")
            or run.get("targets") != [f"{u.unit_id.agent}/{u.unit_id.logical_version}" for u in plan]
            or pointer.get("source_revision") != source_revision or retained != result.to_dict()
            or environment.profile != "daily"
            or self.run.read_completed("environment") != asdict(environment)
        ):
            raise PrivateReportError("private_report_source_mismatch")
        frozen = self.run.read_completed("delivery-inputs", missing_ok=True)
        if frozen is not None and (
            frozen.get("report") != retained or frozen.get("source_revision") != source_revision
            or frozen.get("report_date") != report_date or frozen.get("test_run") is not test_run
        ):
            raise PrivateReportError("private_report_source_mismatch")
        # Recompute under the reviewed plan, rejecting foreign/extra units.
        restore_public_result(retained, allowed_units=plan)
        return {
            "run_id": self.run_id, "profile": "daily",
            "mode": "test" if test_run else "official" if result.team_report_eligible else "failure",
            "report_date": metadata.report_date, "source_revision": source_revision,
            "region": environment.region_display, "result_sha256": _hash(_json(retained)),
            "plan_sha256": _hash(_json({"units": [u.to_dict() for u in plan]})),
            "assessment_sha256": _hash(_json(assessment)),
        }

    def prepare(
        self, root: Path, result: QualityResult, *, allowed_units: tuple[PlannedUnit, ...],
        environment: Environment, source_revision: str, report_date: str, test_run: bool,
    ) -> dict:
        if self.presentation_id is not None:
            raise PrivateReportError("private_report_original_outbox_required")
        identity = self._identity(
            result, allowed_units, environment, source_revision, report_date, test_run,
        )
        previous = self.records.read_completed(self.request_key, missing_ok=True)
        if previous is not None:
            request = self.request()
            if request["identity"] != identity:
                raise PrivateReportError("private_report_source_mismatch")
            return request
        context = load_report_context(root, allowed_units=allowed_units)
        review = RetainedReviewContext(self.runtime, self.run_id, result)
        request = self._build_request(
            result, allowed_units=allowed_units, environment=environment, identity=identity,
            context=context, review=review,
        )
        # All bytes and destination are immutable before any provider call.
        self.records.save_completed(self.request_key, request)
        self.export_local(request)
        return request

    def prepare_test_presentation(
        self, root: Path, result: QualityResult, *, allowed_units: tuple[PlannedUnit, ...],
        environment: Environment, source_revision: str, report_date: str,
    ) -> PrivateReportOutbox:
        """Render only an existing TEST measurement into a separate immutable bundle."""
        if self.presentation_id is not None:
            raise PrivateReportError("private_report_original_outbox_required")
        if self.disabled:
            raise PrivateReportError("private_report_checkpoint_failed")
        original = self.request()
        if original["identity"]["mode"] != "test":
            raise PrivateReportError("private_report_test_presentation_required")
        self.read_receipt(original)
        frozen = self.run.read_completed("delivery-inputs", missing_ok=True)
        if frozen is None:
            raise PrivateReportError("private_report_frozen_inputs_missing")
        identity = self._identity(
            result, allowed_units, environment, source_revision, report_date, True,
        )
        if identity != original["identity"] or (
            frozen.get("region_display") != environment.region_display
            or type(frozen.get("rerun")) is not int
            or frozen["rerun"] != self.run.read_completed("run")["rerun"]
        ):
            raise PrivateReportError("private_report_source_mismatch")
        context = load_report_context(root, allowed_units=allowed_units)
        retained_context = frozen.get("report_context")
        if (
            not isinstance(retained_context, dict)
            or set(retained_context) != {"catalog_root", "units"}
            or not isinstance(retained_context["catalog_root"], str)
            or retained_context["units"] != context.to_private_dict()["units"]
        ):
            raise PrivateReportError("private_report_reviewed_context_changed")
        review = RetainedReviewContext(self.runtime, self.run_id, result)
        if review.provenance() != original["retained_review"]:
            raise PrivateReportError("private_report_retained_review_changed")
        if "presentation" in frozen:
            presentation = frozen["presentation"]
            if not isinstance(presentation, dict) or (
                presentation.get("private_report_artifact") != "presentation/report"
            ):
                raise PrivateReportError("private_report_frozen_inputs_invalid")
            retained_report = self.run.read_artifact("presentation/report")
            if (
                retained_report.get("format") != "markdown" or retained_report.get("private") is not True
                or not isinstance(retained_report.get("markdown"), str)
                or retained_report.get("retained_review") != review.provenance()
            ):
                raise PrivateReportError("private_report_retained_review_changed")
        request = self._build_request(
            result, allowed_units=allowed_units, environment=environment, identity=identity,
            context=context, review=review,
        )
        selected = PrivateReportOutbox(
            self.runtime, self.run_id, presentation_id=request["presentation_id"], clock=self.clock,
        )
        previous, _ = selected._request_record(missing_ok=True)
        if previous is not None:
            if selected.request() != request:
                raise PrivateReportError("private_report_presentation_conflict")
            if request["presentation_id"] == original["presentation_id"]:
                return selected
        try:
            selected.records.save_completed(selected.request_key, request)
            selected.export_local(request)
        except StateError:
            self.disabled = True
            raise
        return selected

    def _build_request(self, result, *, allowed_units, environment, identity, context, review):
        metadata = ReportMetadata(
            identity["report_date"], environment.region_display, identity["source_revision"],
        )
        markdown = render_private_markdown(
            result, allowed_units=allowed_units, review_context=review,
            report_context=context, metadata=metadata,
            delivery_id=self.run_id,
        )
        from . import reporting

        files = {"report.md": markdown, "report.html": markdown_view(markdown)}
        for agent in dict.fromkeys(unit.unit_id.agent for unit in allowed_units):
            detail = render_private_markdown(
                result, allowed_units=allowed_units, review_context=review,
                report_context=context, metadata=metadata,
                delivery_id=self.run_id, agent=agent,
            )
            files[f"agents/{agent}/report.md"] = detail
            files[f"agents/{agent}/report.html"] = markdown_view(detail)
        file_types = _file_types(allowed_units)
        manifest = {
            "schema_version": "1.0", **identity, "renderer": RENDERER,
            "renderer_sha256": _hash(Path(reporting.__file__).read_bytes().replace(b"\r\n", b"\n")),
            "review_sha256": _hash(_json(review.provenance())),
            "files": {name: {
                "sha256": _hash(content.encode("utf-8")), "bytes": len(content.encode("utf-8")),
                "content_type": file_types[name],
            } for name, content in files.items()},
            "access_required": True, "auth_mode": "entra_storage_data_plane",
            "human_validation_available": False,
        }
        presentation_id = _hash(_json(manifest))
        files["manifest.json"] = _json({**manifest, "presentation_id": presentation_id}).decode("utf-8")
        if any(len(content.encode("utf-8")) > MAX_BYTES for content in files.values()):
            raise PrivateReportError("private_report_too_large")
        if any(re.search(r"(?:[?&]|&amp;)sig=", content, re.I) for content in files.values()):
            raise PrivateReportError("private_report_embedded_access_forbidden")
        return {
            "schema_version": "1.0", "identity": identity, "presentation_id": presentation_id,
            "account": _account(environment.storage_account_name), "container": CONTAINER,
            "prefix": f"reports/daily/{identity['mode']}/{self.run_id}/{presentation_id}",
            "plan": [unit.to_dict() for unit in allowed_units], "files": files,
            "retained_review": review.provenance(),
        }

    def request(self) -> dict:
        request, _ = self._request_record()
        try:
            if set(request) != {
                "schema_version", "identity", "presentation_id", "account", "container",
                "prefix", "plan", "files", "retained_review",
            } or request["schema_version"] != "1.0" or request["container"] != CONTAINER:
                raise ValueError
            plan = tuple(PlannedUnit.from_dict(unit) for unit in request["plan"])
            if request["plan"] != [unit.to_dict() for unit in plan]:
                raise ValueError
            pointer = self.run.read("quality-result")
            result = restore_public_result(self.run.read_artifact(pointer["artifact"]), allowed_units=plan)
            environment = Environment(**self.run.read_completed("environment"))
            identity = request["identity"]
            expected = self._identity(
                result, plan, environment, identity["source_revision"],
                identity["report_date"], identity["mode"] == "test",
            )
            if identity != expected or request["account"] != _account(environment.storage_account_name):
                raise ValueError
            files = request["files"]
            if not isinstance(files, dict) or "manifest.json" not in files or any(
                not isinstance(v, str) or len(v.encode("utf-8")) > MAX_BYTES for v in files.values()
            ):
                raise ValueError
            if any(re.search(r"(?:[?&]|&amp;)sig=", content, re.I) for content in files.values()):
                raise ValueError
            manifest = json.loads(files["manifest.json"], object_pairs_hook=_unique_object)
            if not isinstance(manifest, dict):
                raise ValueError
            file_types = _file_types(plan, legacy=manifest.get("renderer") == "private-md-html-v1")
            if set(files) != set(file_types):
                raise ValueError
            presentation_id = manifest.pop("presentation_id")
            if (
                presentation_id != request["presentation_id"] or _hash(_json(manifest)) != presentation_id
                or self.presentation_id is not None and presentation_id != self.presentation_id
                or set(manifest) != set(identity) | {
                    "schema_version", "renderer", "renderer_sha256", "review_sha256", "files",
                    "access_required", "auth_mode", "human_validation_available",
                }
                or any(manifest[k] != v for k, v in identity.items())
                or manifest["schema_version"] != "1.0"
                or manifest["renderer"] not in {RENDERER, "private-md-html-v1"}
                or re.fullmatch(_HASH, manifest["renderer_sha256"]) is None
                or manifest["access_required"] is not True
                or manifest["auth_mode"] != "entra_storage_data_plane"
                or manifest["human_validation_available"] is not False
                or manifest["review_sha256"] != _hash(_json(request["retained_review"]))
                or manifest["files"] != {name: {
                    "sha256": _hash(files[name].encode("utf-8")),
                    "bytes": len(files[name].encode("utf-8")), "content_type": file_types[name],
                } for name in files if name != "manifest.json"}
                or request["prefix"] != f"reports/daily/{identity['mode']}/{self.run_id}/{presentation_id}"
                or files["manifest.json"] != _json({
                    **manifest, "presentation_id": presentation_id,
                }).decode("utf-8")
            ):
                raise ValueError
            _key(request["prefix"] + "/manifest.json")
        except (KeyError, TypeError, ValueError):
            raise PrivateReportError("private_report_request_invalid") from None
        return request

    def _base(self, request):
        return self.run_id + "/" + request["presentation_id"]

    def export_local(self, request):
        directory = self.records.directory / "artifacts" / self.run_id / request["presentation_id"]
        for name, content in request["files"].items():
            _write_local(self.runtime, directory / name, content.encode("utf-8"))
        return directory

    def status(self, request=None):
        request = self.request() if request is None else request
        base = self._base(request)
        state = self.records.read(base + "/status", missing_ok=True)
        if state is None:
            state = {
                "status": "pending", "code": "ok",
                "latest": "pending" if request["identity"]["mode"] == "official" else "not_applicable",
            }
        if (
            set(state) != {"status", "code", "latest"}
            or not isinstance(state["status"], str)
            or state["status"] not in {"pending", "delivered", "conflict"}
            or not isinstance(state["latest"], str)
            or state["latest"] not in {"pending", "not_applicable", "current", "advanced", "superseded", "conflict"}
            or not isinstance(state["code"], str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", state["code"]) is None
        ):
            raise PrivateReportError("private_report_status_invalid")
        directory = self.records.directory / "artifacts" / self.run_id / request["presentation_id"]
        receipt = self.read_receipt(request, missing_ok=True)
        if state["status"] == "delivered" and receipt is None:
            raise PrivateReportError("private_report_receipt_missing")
        return {
            **state, "presentation_id": request["presentation_id"],
            "request_path": str(self.records._path("completed", self._request_record()[1])),
            "receipt_path": str(self.records._path("completed", base + "/receipt")) if receipt else None,
            "markdown_path": str(directory / "report.md"), "html_path": str(directory / "report.html"),
            "manifest_path": str(directory / "manifest.json"),
            "access_required": True, "auth_mode": "entra_storage_data_plane",
            "human_validation_available": False,
        }

    def read_receipt(self, request=None, *, missing_ok=False):
        """Validate immutable bundle evidence independently of latest delivery."""
        request = self.request() if request is None else request
        receipt = self.records.read_completed(self._base(request) + "/receipt", missing_ok=True)
        if receipt is None:
            if not missing_ok:
                raise PrivateReportError("private_report_receipt_missing")
        elif receipt != self._receipt_value(request):
            raise PrivateReportError("private_report_receipt_invalid")
        return receipt

    def _receipt_value(self, request):
        from .report_links import authenticated_storage_references

        return {
            "schema_version": "1.0", "presentation_id": request["presentation_id"],
            "identity": request["identity"],
            "files": {name: {
                "sha256": _hash(content.encode("utf-8")), "bytes": len(content.encode("utf-8")),
            } for name, content in request["files"].items()},
            "storage_references": authenticated_storage_references(
                request["account"], CONTAINER, request["prefix"],
            ),
        }

    def flush(self, client: ReportBlobPort, *, seconds=FLUSH_SECONDS, read_only=False) -> dict:
        """Reconcile before PUT; read_only never writes Azure, only local receipts."""
        if self.disabled:
            raise PrivateReportError("private_report_checkpoint_failed")
        if not 0 < seconds <= FLUSH_SECONDS:
            raise PrivateReportError("private_report_deadline_invalid")
        if type(read_only) is not bool:
            raise PrivateReportError("private_report_mode_invalid")
        if not self._flush_lock.acquire(blocking=False):
            raise PrivateReportError("private_report_busy")
        try:
            return self._flush(client, seconds=seconds, read_only=read_only)
        except StateError:
            self.disabled = True
            raise
        finally:
            self._flush_lock.release()

    def _flush(self, client, *, seconds, read_only):
        request = self.request()
        self.export_local(request)
        base = self._base(request)
        old = self.records.read(base + "/status", missing_ok=True)
        if old and old["status"] in {"delivered", "conflict"}:
            return self.status(request)
        deadline = self.clock() + seconds
        def timeout():
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise PrivateReportError("private_report_deadline")
            return min(CALL_SECONDS, remaining)
        state = {
            "status": "pending", "code": "ok",
            "latest": "pending" if request["identity"]["mode"] == "official" else "not_applicable",
        }
        self.records.save_progress(base + "/status", state)
        try:
            client.verify_private(timeout=timeout())
            receipts = {}
            names = ["report.md", "report.html"] + sorted(
                name for name in request["files"] if name.startswith("agents/")
            ) + ["manifest.json"]
            for name in names:
                key = request["prefix"] + "/" + name
                data = request["files"][name].encode("utf-8")
                snapshot = client.read(key, timeout=timeout())
                if snapshot is None and not read_only:
                    self.records.save_progress(base + "/intent", {
                        "key": key, "sha256": _hash(data), "state": "unknown",
                    })
                    call_timeout = timeout()
                    try:
                        client.write(
                            key, data, content_type=_TYPES[name.rsplit("/", 1)[-1]], etag=None, timeout=call_timeout,
                        )
                    except (PrivateReportError, OSError):
                        pass
                    snapshot = client.read(key, timeout=timeout())
                if snapshot is None:
                    raise PrivateReportError("private_report_write_unresolved")
                if snapshot.data != data:
                    raise PrivateReportError("private_report_content_conflict")
                receipts[name] = {"sha256": _hash(snapshot.data), "bytes": len(snapshot.data)}
            receipt = self._receipt_value(request)
            if receipt["files"] != receipts:
                raise PrivateReportError("private_report_content_conflict")
            self.records.save_completed(base + "/receipt", receipt)
            if request["identity"]["mode"] == "official":
                state["latest"] = self._latest(client, request, timeout, read_only)
            state["status"] = "delivered" if state["latest"] != "pending" else "pending"
        except (PrivateReportError, OSError) as error:
            state["code"] = error.code if isinstance(error, PrivateReportError) else "private_report_unavailable"
            if state["code"] in {"private_report_content_conflict", "private_report_latest_conflict"}:
                state["status"] = "conflict"
            if state["code"] == "private_report_latest_conflict":
                state["latest"] = "conflict"
        self.records.save_progress(base + "/status", state)
        return self.status(request)

    def _latest(self, client, request, timeout, read_only):
        desired = {
            "schema_version": "1.0", "report_date": request["identity"]["report_date"],
            "run_id": self.run_id, "presentation_id": request["presentation_id"],
            "manifest_key": request["prefix"] + "/manifest.json",
            "manifest_sha256": _hash(request["files"]["manifest.json"].encode("utf-8")),
        }
        data = _json(desired)
        # CAS conflicts permit one bounded reread, never an unconditional replace.
        for _ in range(2):
            current = client.read(LATEST, timeout=timeout())
            if current is not None:
                try:
                    value = json.loads(current.data, object_pairs_hook=_unique_object)
                    if set(value) != set(desired) or value["schema_version"] != "1.0":
                        raise ValueError
                    day = date.fromisoformat(value["report_date"]).isoformat()
                    if (
                        value["run_id"] != "daily-" + day
                        or re.fullmatch(_HASH, value["presentation_id"]) is None
                        or re.fullmatch(_HASH, value["manifest_sha256"]) is None
                        or value["manifest_key"] != (
                            f"reports/daily/official/{value['run_id']}/{value['presentation_id']}/manifest.json"
                        )
                    ):
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    raise PrivateReportError("private_report_latest_conflict") from None
                if value == desired:
                    return "current"
                if day > desired["report_date"]:
                    return "superseded"
                if day == desired["report_date"]:
                    raise PrivateReportError("private_report_latest_conflict")
            if read_only:
                return "pending"
            self.records.save_progress(self._base(request) + "/latest-intent", {
                "sha256": _hash(data), "state": "unknown",
                "etag": current.etag if current else None,
            })
            call_timeout = timeout()
            try:
                client.write(
                    LATEST, data, content_type="application/json",
                    etag=current.etag if current else None, timeout=call_timeout,
                )
            except (PrivateReportError, OSError):
                pass
            observed = client.read(LATEST, timeout=timeout())
            if observed is not None and observed.data == data:
                return "advanced"
        return "pending"


def flush_private_report(runtime, run_id, *, read_only=False, blob_factory=None):
    """Publication-only recovery: no catalog refresh, rendering or email access."""
    from .events import RunLogger

    outbox = PrivateReportOutbox(runtime, run_id)
    logger = RunLogger(outbox.run.directory, test_run=True)
    client = None
    try:
        logger.emit("started", stage="outbox", code="private_report_reconciliation")
        request = outbox.request()
        client = (blob_factory or AzurePrivateReportBlob)(request["account"])
        status = outbox.flush(client, read_only=read_only)
        outbox.run.save_progress("private-publication", status)
        logger.emit(
            "completed" if status["status"] == "delivered" else "warning",
            stage="outbox", code=status.get("code", "private_report_pending"),
        )
        if logger.health_warnings:
            status["warnings"] = ["logging_failed"]
        return status
    except (QualityError, OSError) as error:
        logger.emit(
            "warning", stage="outbox",
            code=error.code if isinstance(error, QualityError) else "private_report_unavailable",
        )
        raise
    finally:
        try:
            if client is not None:
                try:
                    client.close()
                except (QualityError, OSError):
                    logger.emit("warning", stage="outbox", code="private_report_close_failed")
        finally:
            logger.close()
