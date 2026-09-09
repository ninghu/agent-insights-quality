"""Private, versioned read-only access grants. Never publish SAS in report blobs.

Only the access record and prepared email/explicit local access preview contain
the URLs. A delegation key stays in provider memory. Existing revisions, including
expired ones, are immutable; an interrupted grant requires an explicit new revision.
"""

from __future__ import annotations

from base64 import b64decode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import re
import time
from urllib.parse import parse_qsl, urlsplit

from .private_publication import (
    AzurePrivateReportBlob, CALL_SECONDS, CONTAINER, FLUSH_SECONDS, PrivateReportError,
    PrivateReportOutbox, _account, _key, _write_local,
)
from .state import StateError, _encode, _parts

MAX_LIFETIME = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=5)
_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_HASH = r"[0-9a-f]{64}"
_SAS_FIELDS = {
    "sv", "spr", "st", "se", "sr", "sp", "skoid", "sktid", "skt", "ske", "sks", "skv", "sig",
}


class ReportAccessError(PrivateReportError):
    """No provider text, URL, signature or key is a safe error message."""


def utc_now():
    return datetime.now(timezone.utc)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ReportAccessError("report_access_clock_invalid")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _stamp(value):
    return _utc(value).isoformat().replace("+00:00", "Z")


def _time(value):
    try:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        if _stamp(parsed) != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise ReportAccessError("report_access_time_invalid") from None


def _sas_stamp(value):
    # Azure may return zero fractional seconds on delegation-key timestamps.
    if not isinstance(value, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.0{1,7})?(?:Z|\+00:00)", value,
    ) is None:
        raise ReportAccessError("report_access_time_invalid")
    try:
        return _stamp(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        raise ReportAccessError("report_access_time_invalid") from None


def validate_read_sas(url, *, account, blob_key, starts_at, expires_at):
    """Validate exact blob scope and delegation-SAS fields, never account SAS."""
    _account(account)
    _key(blob_key)
    if "/agents/" not in blob_key or not blob_key.endswith(("/report.html", "/report.md")):
        raise ReportAccessError("report_access_blob_scope_invalid")
    try:
        if (
            not isinstance(url, str) or len(url) > 4096 or not url.isascii()
            or any(character.isspace() or ord(character) < 33 for character in url)
        ):
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or parsed.netloc != f"{account}.blob.core.windows.net"
            or parsed.path != f"/{CONTAINER}/{blob_key}" or parsed.fragment
        ):
            raise ValueError
        pairs = parse_qsl(parsed.query, strict_parsing=True, keep_blank_values=True)
        query = dict(pairs)
        if len(query) != len(pairs) or set(query) != _SAS_FIELDS:
            raise ValueError
        if (
            query["sp"] != "r" or query["sr"] != "b" or query["spr"] != "https"
            or query["sks"] != "b"
            or _sas_stamp(query["st"]) != starts_at or _sas_stamp(query["skt"]) != starts_at
            or _sas_stamp(query["se"]) != expires_at or _sas_stamp(query["ske"]) != expires_at
            or any(re.fullmatch(_UUID, query[k]) is None for k in ("skoid", "sktid"))
            or any(re.fullmatch(r"\d{4}-\d{2}-\d{2}", query[k]) is None for k in ("sv", "skv"))
            or len(b64decode(query["sig"], validate=True)) != 32
        ):
            raise ValueError
        start, end = _time(starts_at), _time(expires_at)
        if not timedelta(0) < end - start <= MAX_LIFETIME:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ReportAccessError("report_access_sas_invalid") from None
    return url


def validate_delegation_key(key, *, starts_at, expires_at):
    if (
        _sas_stamp(getattr(key, "signed_start", None)) != starts_at
        or _sas_stamp(getattr(key, "signed_expiry", None)) != expires_at
        or getattr(key, "signed_service", None) != "b"
        or re.fullmatch(_UUID, str(getattr(key, "signed_oid", ""))) is None
        or re.fullmatch(_UUID, str(getattr(key, "signed_tid", ""))) is None
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(getattr(key, "signed_version", ""))) is None
        or not timedelta(0) < _time(expires_at) - _time(starts_at) <= MAX_LIFETIME
    ):
        raise ReportAccessError("report_access_delegation_scope_invalid")


def _targets(request):
    agents = tuple(dict.fromkeys(unit["unit_id"]["agent"] for unit in request["plan"]))
    keys = {}
    for agent in agents:
        keys[agent] = {}
        for label, extension in (("html", "html"), ("markdown", "md")):
            name = f"agents/{agent}/report.{extension}"
            if name not in request["files"]:
                raise ReportAccessError("report_access_agent_files_missing")
            keys[agent][label] = _key(request["prefix"] + "/" + name)
    return keys


def validate_descriptor(value, delivery_id):
    if (
        not isinstance(value, dict) or set(value) != {"record_key", "sha256", "expires_at"}
        or not isinstance(value["record_key"], str)
        or re.fullmatch(
            re.escape(delivery_id) + rf"/{_HASH}/access/[a-z0-9][a-z0-9_-]{{0,79}}",
            value["record_key"],
        ) is None
        or re.fullmatch(_HASH, str(value["sha256"])) is None
    ):
        raise ReportAccessError("report_access_descriptor_invalid")
    _parts(value["record_key"])
    _time(value["expires_at"])


class VerifiedReportAccess:
    """No repr/to_dict exposes tokens; only explicit private accessors do."""

    def __init__(self, runtime, record_key, *, descriptor=None):
        parts = _parts(record_key)
        if len(parts) != 4 or parts[2] != "access":
            raise ReportAccessError("report_access_descriptor_invalid")
        outbox = PrivateReportOutbox(runtime, parts[0], presentation_id=parts[1])
        request = outbox.request()
        outbox.read_receipt(request)
        try:
            value = outbox.records.read_completed(record_key, missing_ok=True)
        except StateError:
            raise ReportAccessError("report_access_record_invalid") from None
        if value is None:
            raise ReportAccessError("report_access_record_missing")
        expected_keys = {
            "schema_version", "run_id", "presentation_id", "revision", "account",
            "container", "issued_at", "starts_at", "expires_at", "links",
        }
        if (
            set(value) != expected_keys or value["schema_version"] != "1.0"
            or value["run_id"] != outbox.run_id or value["presentation_id"] != parts[1]
            or value["presentation_id"] != request["presentation_id"] or value["revision"] != parts[3]
            or value["account"] != request["account"] or value["container"] != CONTAINER
        ):
            raise ReportAccessError("report_access_record_invalid")
        issued, start, end = (_time(value[name]) for name in ("issued_at", "starts_at", "expires_at"))
        if start != issued - CLOCK_SKEW or end != start + MAX_LIFETIME:
            raise ReportAccessError("report_access_lifetime_invalid")
        keys = _targets(request)
        if not isinstance(value["links"], dict) or set(value["links"]) != set(keys):
            raise ReportAccessError("report_access_record_invalid")
        for agent, links in value["links"].items():
            if not isinstance(links, dict) or set(links) != {"html", "markdown"}:
                raise ReportAccessError("report_access_record_invalid")
            for kind, url in links.items():
                validate_read_sas(
                    url, account=value["account"], blob_key=keys[agent][kind],
                    starts_at=value["starts_at"], expires_at=value["expires_at"],
                )
        self._value, self._records, self._key = value, outbox.records, record_key
        self._outbox = outbox
        self.descriptor = {
            "record_key": record_key, "sha256": sha256(_encode(value)).hexdigest(),
            "expires_at": value["expires_at"],
        }
        if descriptor is not None and descriptor != self.descriptor:
            raise ReportAccessError("report_access_descriptor_mismatch")

    @property
    def expires_at(self):
        return self._value["expires_at"]

    def expired(self, *, now=None):
        return _utc(now or utc_now()) >= _time(self.expires_at)

    def for_agents(self, agents, run_id):
        self._outbox.read_receipt()
        if set(agents) != set(self._value["links"]) or run_id != self._value["run_id"]:
            raise ReportAccessError("report_access_agent_scope_mismatch")
        return {agent: dict(links) for agent, links in self._value["links"].items()}

    def status(self, *, now=None):
        self._outbox.read_receipt()
        expired = self.expired(now=now)
        return {
            "status": "expired_needs_explicit_new_access_revision" if expired else "ready",
            "access_record_path": str(self._records._path("completed", self._key)),
            "expires_at": self.expires_at, "access_revision": self._value["revision"],
            "auth_mode": "user_delegation_sas", "human_validation_available": not expired,
            "forwarding_warning": "Anyone holding a link can read that file.",
        }

    def preview(self):
        links = self.for_agents(self._value["links"], self._value["run_id"])
        body = "".join(
            f"<tr><td>{escape(agent)}</td><td>"
            f'<a href="{escape(values["html"], quote=True)}">View report</a></td><td>'
            f'<a href="{escape(values["markdown"], quote=True)}">Download MD</a></td></tr>'
            for agent, values in links.items()
        )
        content = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            "<title>Private access refresh - not sent</title></head><body>"
            "<h1>Private access refresh - NOT SENT</h1>"
            "<p>The original prepared email and measurement are unchanged.</p>"
            f"<p>Links expire {escape(self.expires_at)} (up to 7 days). "
            "Anyone holding a link can read that file; forward carefully.</p>"
            f"<table>{body}</table></body></html>"
        ).encode("utf-8")
        directory = self._records.directory / "artifacts"
        path = directory.joinpath(*_parts(self._key)) / "access.html"
        _write_local(self._records._runtime, path, content)
        return {**self.status(), "preview_path": str(path)}


def read_report_access(runtime, descriptor, *, delivery_id):
    validate_descriptor(descriptor, delivery_id)
    return VerifiedReportAccess(runtime, descriptor["record_key"], descriptor=descriptor)


def prepare_report_access(outbox, client, *, revision="initial", now=None, clock=time.monotonic):
    """A single grant revision. Hold ownership; never retry interrupted signing."""
    if outbox.disabled:
        raise ReportAccessError("report_access_checkpoint_failed")
    if not outbox._flush_lock.acquire(blocking=False):
        raise ReportAccessError("report_access_busy")
    try:
        return _prepare_report_access(outbox, client, revision=revision, now=now, clock=clock)
    except StateError:
        outbox.disabled = True
        raise
    finally:
        outbox._flush_lock.release()


def _prepare_report_access(outbox, client, *, revision, now, clock):
    if len(_parts(revision)) != 1:
        raise ReportAccessError("report_access_revision_invalid")
    request = outbox.request()
    base = outbox.run_id + "/" + request["presentation_id"]
    record_key = base + "/access/" + revision
    if outbox.records.read_completed(record_key, missing_ok=True) is not None:
        return VerifiedReportAccess(outbox.runtime, record_key)
    if not outbox.status(request)["receipt_path"]:
        raise ReportAccessError("report_access_publication_incomplete")
    intent_key = base + "/access-intents/" + revision
    if outbox.records.read_completed(intent_key, missing_ok=True) is not None:
        raise ReportAccessError("report_access_interrupted_needs_new_revision")
    issued = _utc(now or utc_now())
    start = issued - CLOCK_SKEW
    end = start + MAX_LIFETIME
    keys = _targets(request)
    deadline = clock() + FLUSH_SECONDS
    def timeout():
        left = deadline - clock()
        if left <= 0:
            raise ReportAccessError("report_access_deadline")
        return min(CALL_SECONDS, left)
    client.verify_private(timeout=timeout())
    for links in keys.values():
        for key in links.values():
            snapshot = client.read(key, timeout=timeout())
            expected = request["files"][key.removeprefix(request["prefix"] + "/")].encode("utf-8")
            if snapshot is None or snapshot.data != expected:
                raise ReportAccessError("report_access_blob_unverified")
    intent = {
        "issued_at": _stamp(issued), "starts_at": _stamp(start), "expires_at": _stamp(end),
    }
    outbox.records.save_completed(intent_key, intent)
    flat_keys = [key for links in keys.values() for key in links.values()]
    signed = client.sign_read_links(
        flat_keys, starts_at=intent["starts_at"], expires_at=intent["expires_at"], timeout=timeout(),
    )
    if not isinstance(signed, dict) or set(signed) != set(flat_keys):
        raise ReportAccessError("report_access_sas_invalid")
    links = {
        agent: {kind: validate_read_sas(
            signed[key], account=request["account"], blob_key=key,
            starts_at=intent["starts_at"], expires_at=intent["expires_at"],
        ) for kind, key in values.items()} for agent, values in keys.items()
    }
    outbox.records.save_completed(record_key, {
        "schema_version": "1.0", "run_id": outbox.run_id, "presentation_id": request["presentation_id"],
        "revision": revision, "account": request["account"], "container": CONTAINER,
        **intent, "links": links,
    })
    return VerifiedReportAccess(outbox.runtime, record_key)


def refresh_report_access(runtime, run_id, *, revision, blob_factory=None):
    """Explicit versioned access/local preview only. Never prepares or sends mail."""
    from .events import RunLogger

    if revision == "initial":
        raise ReportAccessError("report_access_new_revision_required")
    outbox = PrivateReportOutbox(runtime, run_id)
    request = outbox.request()
    logger = RunLogger(outbox.run.directory, test_run=True)
    client = None
    try:
        logger.emit("started", stage="outbox", code="report_access_refresh")
        client = (blob_factory or AzurePrivateReportBlob)(request["account"])
        access = prepare_report_access(outbox, client, revision=revision)
    except (PrivateReportError, StateError, OSError) as error:
        logger.emit(
            "warning", stage="outbox",
            code=error.code if isinstance(error, (PrivateReportError, StateError)) else "report_access_unavailable",
        )
        raise
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            logger.close()
    return access.preview()
