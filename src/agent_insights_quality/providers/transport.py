from __future__ import annotations

import asyncio
import http.client
import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from contextvars import Context
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_insights_quality.contracts import JsonObject
from agent_insights_quality.errors import QualityError
from agent_insights_quality.invocation_context import validate_traceparent
from agent_insights_quality.providers.cancellation import drain_on_cancel

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
ARM_SCOPE = "https://management.azure.com/.default"
TRANSIENT = {408, 429, 500, 502, 503, 504}
AZURE_DEVOPS_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"
HOSTED_FEATURES = "HostedAgents=V1Preview"


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str = field(repr=False)
    scope: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout: float = 300


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes = field(default=b"", repr=False)

    def header(self, name: str) -> str | None:
        return next(
            (
                value
                for key, value in self.headers.items()
                if key.lower() == name.lower()
            ),
            None,
        )

    def object(self) -> JsonObject:
        try:
            value = json.loads(self.body) if self.body else {}
        except (ValueError, UnicodeError):
            raise QualityError(
                "provider_invalid_json", request_accepted=True, status=self.status
            ) from None
        if not isinstance(value, dict):
            raise QualityError(
                "provider_invalid_object", request_accepted=True, status=self.status
            )
        return value


class Transport(Protocol):
    async def send(self, request: HttpRequest) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _explicit_traceparent(headers: Mapping[str, str]) -> str | None:
    parents = [value for key, value in headers.items() if key.casefold() == "traceparent"]
    if not parents:
        return None
    if len(parents) != 1 or any(key.casefold() in {"baggage", "tracestate"} for key in headers):
        raise QualityError("trace_context_headers_conflict", request_accepted=False)
    validate_traceparent(parents[0])
    return parents[0]


def _suppress_http_tracing():
    try:
        from opentelemetry.instrumentation.utils import suppress_instrumentation
    except ModuleNotFoundError as error:
        if error.name in {"opentelemetry", "opentelemetry.instrumentation", "opentelemetry.instrumentation.utils"}:
            return nullcontext()
        raise QualityError("trace_context_instrumentation_unavailable", request_accepted=False) from None
    except ImportError:
        raise QualityError("trace_context_instrumentation_unavailable", request_accepted=False) from None
    return suppress_instrumentation()


class _PinnedTraceRequest(urllib.request.Request):
    def __init__(self, *args, traceparent: str, **kwargs):
        self._traceparent = traceparent
        super().__init__(*args, **kwargs)

    def _check_context(self, key: str, value: str) -> None:
        if key.casefold() in {"baggage", "tracestate"} or (
            key.casefold() == "traceparent" and value != self._traceparent
        ):
            raise QualityError("trace_context_header_overwrite", request_accepted=False)

    def add_header(self, key, val):
        self._check_context(key, val)
        super().add_header(key, val)

    def add_unredirected_header(self, key, val):
        self._check_context(key, val)
        super().add_unredirected_header(key, val)


class AzureHttpTransport:
    """Current Azure CLI identity; credentials are acquired only when a request is sent."""

    def __init__(self, credential: Any = None) -> None:
        self._credential = credential
        self._token_providers: dict[str, Callable[[], str]] = {}
        self._credential_lock = threading.Lock()

    async def send(self, request: HttpRequest) -> HttpResponse:
        if _explicit_traceparent(request.headers):
            return await drain_on_cancel(asyncio.to_thread(Context().run, self._isolated_send, request))
        return await drain_on_cancel(asyncio.to_thread(self._send, request))

    def _isolated_send(self, request: HttpRequest) -> HttpResponse:
        with _suppress_http_tracing():
            return self._send(request)

    def _send(self, request: HttpRequest) -> HttpResponse:
        validate_url(request.url)
        if request.scope not in {FOUNDRY_SCOPE, ARM_SCOPE, AZURE_DEVOPS_SCOPE}:
            raise QualityError("provider_scope_invalid", request_accepted=False)
        token = self._bearer_token(request.scope)
        headers = dict(request.headers)
        headers["Authorization"] = f"Bearer {token}"
        parent = _explicit_traceparent(headers)
        request_type = _PinnedTraceRequest if parent else urllib.request.Request
        wire = request_type(
            request.url, data=request.body, headers=headers, method=request.method,
            **({"traceparent": parent} if parent else {}),
        )
        try:
            try:
                response = urllib.request.build_opener(_NoRedirect()).open(
                    wire, timeout=request.timeout
                )
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return HttpResponse(
                    response.status, dict(response.headers), response.read()
                )
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise QualityError("provider_no_response", request_accepted=None) from None

    def _bearer_token(self, scope: str) -> str:
        try:
            from azure.core.exceptions import AzureError
            from azure.identity import AzureCliCredential, get_bearer_token_provider
        except ImportError:
            raise QualityError(
                "azure_identity_unavailable", request_accepted=False
            ) from None
        # The SDK callable owns expiry/refresh caching; serialize refresh across scopes
        # because concurrent Azure CLI processes contend for the same local identity.
        with self._credential_lock:
            if self._credential is None:
                self._credential = AzureCliCredential(process_timeout=60)
            provider = self._token_providers.get(scope)
            if provider is None:
                provider = get_bearer_token_provider(self._credential, scope)
                self._token_providers[scope] = provider
            try:
                return provider()
            except AzureError:
                raise QualityError(
                    "azure_authentication_failed", request_accepted=False
                ) from None


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(ord(char) < 33 for char in url)
    ):
        raise QualityError("provider_url_invalid", request_accepted=False)


def segment(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualityError("provider_identity_missing", request_accepted=False)
    return urllib.parse.quote(value, safe="")


def encode(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise QualityError("provider_payload_invalid", request_accepted=False) from None


def check_status(
    response: HttpResponse,
    expected: set[int],
    *,
    read_only: bool = False,
    idempotent: bool = False,
) -> None:
    if response.status in expected:
        return
    accepted = (
        False if 400 <= response.status < 500 and response.status != 408 else None
    )
    raise QualityError(
        "provider_http_error",
        status=response.status,
        request_accepted=accepted,
        retryable=response.status in TRANSIENT
        and (read_only or idempotent or accepted is False),
    )


class JsonClient:
    def __init__(
        self,
        endpoint: str,
        transport: Transport,
        *,
        scope: str = FOUNDRY_SCOPE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_attempts: int = 3,
    ) -> None:
        validate_url(endpoint)
        if read_attempts < 1:
            raise QualityError("provider_retry_limit_invalid")
        self.endpoint = endpoint.rstrip("/")
        self.transport = transport
        self.scope = scope
        self.sleep = sleep
        self.read_attempts = read_attempts

    def url(self, path: str, *, api_version: str | None = "v1") -> str:
        url = self.endpoint + path
        if path.startswith(("https://", "http://", "//")):
            url = path
        validate_url(url)
        base, parsed = urllib.parse.urlsplit(self.endpoint), urllib.parse.urlsplit(url)
        if (
            parsed.netloc != base.netloc
            or not (parsed.path == base.path or parsed.path.startswith(base.path + "/"))
            or any(
                part in {".", ".."}
                for part in urllib.parse.unquote(parsed.path)
                .replace("\\", "/")
                .split("/")
            )
        ):
            raise QualityError("provider_link_out_of_scope", request_accepted=False)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if api_version and not any(key == "api-version" for key, _ in query):
            query.append(("api-version", api_version))
        return urllib.parse.urlunsplit(
            parsed._replace(query=urllib.parse.urlencode(query))
        )

    def next_url(self, current: str, link: str) -> str:
        if not isinstance(link, str) or not link:
            raise QualityError("provider_pagination_invalid")
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(current, link))
        query = dict(urllib.parse.parse_qsl(parsed.query))
        previous = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(current).query))
        for key in ("include_details", "api-version"):
            if key in previous:
                query[key] = previous[key]
        return self.url(
            urllib.parse.urlunsplit(
                parsed._replace(query=urllib.parse.urlencode(query))
            )
        )

    async def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        hosted: bool = False,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        api_version: str | None = "v1",
        timeout: float = 300,
    ) -> HttpResponse:
        if not math.isfinite(timeout) or timeout <= 0:
            raise QualityError("provider_timeout_invalid", request_accepted=False)
        values = {"Accept": "application/json"}
        if payload is not None:
            if body is not None:
                raise QualityError("provider_payload_conflict", request_accepted=False)
            body = encode(payload)
            values["Content-Type"] = "application/json"
        if hosted:
            values["Foundry-Features"] = HOSTED_FEATURES
        values.update(headers or {})
        if any(
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
            or not isinstance(value, str)
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
            for key, value in values.items()
        ):
            raise QualityError("provider_header_invalid", request_accepted=False)
        request = HttpRequest(
            method,
            self.url(path, api_version=api_version),
            self.scope,
            values,
            body,
            timeout,
        )
        attempts = self.read_attempts if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self.transport.send(request)
            except (OSError, QualityError) as error:
                if (
                    isinstance(error, QualityError)
                    and error.request_accepted is not None
                ):
                    raise
                if attempt + 1 == attempts:
                    raise QualityError(
                        "provider_no_response",
                        retryable=method == "GET",
                        request_accepted=None,
                    ) from None
            else:
                if response.status not in TRANSIENT or attempt + 1 == attempts:
                    return response
            await self.sleep(min(2**attempt, 8))
        raise AssertionError("Unreachable retry state")

    async def object(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> JsonObject:
        response = await self.request(method, path, payload, **kwargs)
        check_status(response, {200, 201, 202, 204}, read_only=method == "GET")
        return response.object()

    async def pages(
        self,
        path: str,
        *,
        hosted: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> list[JsonObject]:
        url = self.url(path)
        seen: set[str] = set()
        values: list[JsonObject] = []
        while url:
            if url in seen:
                raise QualityError("provider_pagination_cycle")
            seen.add(url)
            page = await self.object("GET", url, hosted=hosted, headers=headers)
            items = next(
                (page[key] for key in ("data", "value", "items") if key in page), None
            )
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise QualityError("provider_pagination_invalid")
            values.extend(items)
            link = (
                page.get("next_link")
                or page.get("nextLink")
                or page.get("@odata.nextLink")
            )
            if link:
                url = self.next_url(url, link)
            elif page.get("has_more"):
                last = items[-1].get("id") if items else None
                if not isinstance(last, str) or not last:
                    raise QualityError("provider_pagination_invalid")
                parsed = urllib.parse.urlsplit(url)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                query["after"] = last
                url = self.url(
                    urllib.parse.urlunsplit(
                        parsed._replace(query=urllib.parse.urlencode(query))
                    )
                )
            else:
                url = ""
        return values
