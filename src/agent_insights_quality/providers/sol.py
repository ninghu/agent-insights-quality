from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from agent_insights_quality.contracts import Environment, JsonObject
from agent_insights_quality.errors import QualityError
from agent_insights_quality.state import StateError
from agent_insights_quality.providers.transport import (
    AzureHttpTransport,
    HttpResponse,
    JsonClient,
    Transport,
    check_status,
    encode,
)

_FALLBACK_COOLDOWN_SECONDS = 60
_MAX_RETRY_WAIT_SECONDS = 300
_MAX_TOTAL_WAIT_SECONDS = 600


def _rate_headers(response: HttpResponse) -> dict[str, str]:
    values = {}
    for name in (
        "retry-after", "retry-after-ms",
        "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
    ):
        raw = response.header(name)
        if not isinstance(raw, str) or len(raw) > 64:
            continue
        value = raw.strip()
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            values[name] = value
        elif name.startswith("x-ratelimit-reset-") and re.fullmatch(
            r"(?:[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d))+", value,
        ):
            values[name] = value
        elif name == "retry-after":
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is not None:
                    values[name] = format_datetime(date.astimezone(UTC), usegmt=True)
            except (ValueError, TypeError, OverflowError):
                continue
    return values


def _retry_delay(response: HttpResponse, wall_time: float) -> float:
    delays = []
    for name, scale in (("retry-after-ms", 1000), ("retry-after", 1)):
        raw = response.header(name)
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if len(value) > 64:
            if value.isascii() and value.isdigit() and value.lstrip("0"):
                return math.inf
            continue
        try:
            delay = float(value) / scale
        except ValueError:
            if name != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    continue
                delay = date.timestamp() - wall_time
            except (ValueError, TypeError, OverflowError, OSError):
                continue
        if math.isfinite(delay) and delay > 0:
            delays.append(delay)
    # When both headers exist, never retry earlier than either advertised reset.
    return max(delays, default=_FALLBACK_COOLDOWN_SECONDS)


def _response_detail(response: HttpResponse) -> JsonObject:
    try:
        return response.object()
    except QualityError:
        return {"raw_body": response.body.decode("utf-8", errors="replace")}


def _unique_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    value: JsonObject = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON property")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _wire_schema(schema: Mapping[str, Any]) -> JsonObject:
    """Omit provider-unsupported uniqueness checks only at JSON Schema nodes."""
    projected = deepcopy(dict(schema))

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node.pop("uniqueItems", None)
        for keyword in ("properties", "patternProperties", "$defs", "definitions",
                        "dependentSchemas", "dependencies"):
            children = node.get(keyword)
            if isinstance(children, dict):
                for child in children.values():
                    visit(child)
        for keyword in ("items", "prefixItems", "allOf", "anyOf", "oneOf",
                        "additionalItems", "additionalProperties", "unevaluatedItems",
                        "unevaluatedProperties", "contains", "propertyNames",
                        "not", "if", "then", "else", "contentSchema"):
            child = node.get(keyword)
            if isinstance(child, list):
                for item in child:
                    visit(item)
            else:
                visit(child)

    visit(projected)
    return projected


class SolResponseError(QualityError):
    """The response is private diagnostic data; the exception text is only a safe code."""

    def __init__(
        self,
        code: str,
        response: Mapping[str, Any],
        *,
        status: int,
        request_accepted: bool | None = True,
        retryable: bool = False,
        private_detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code, request_accepted=request_accepted, status=status, retryable=retryable
        )
        self.response = response
        self.private_detail = dict(private_detail) if private_detail is not None else None


class AzureSol:
    def __init__(
        self,
        environment: Environment,
        *,
        transport: Transport | None = None,
        deployment: str = "sol-assessment",
        attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        if not deployment or not 1 <= attempts <= 5:
            raise QualityError("sol_configuration_invalid")
        self._client = JsonClient(
            environment.project_endpoint,
            transport if transport is not None else AzureHttpTransport(),
            sleep=sleep,
        )
        self.deployment = deployment
        self.attempts = attempts
        self.sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._cooldown_until = 0.0
        self._cooldown_response: HttpResponse | None = None
        self._recovery_gate = asyncio.Lock()
        self._recovery_calls = 0
        self.observer = observer
        self.observer_warnings: set[str] = set()

    def _notify(self, event: Mapping[str, Any]) -> None:
        if self.observer is None:
            return
        try:
            self.observer(event)
        except (StateError, OSError):
            self.observer_warnings.add("performance_persistence_failed")
            try:
                sys.stderr.write("WARNING performance_persistence_failed\n")
            except (OSError, ValueError):
                pass

    @contextmanager
    def _observed(self, kind: str, **fields):
        if self.observer is None:
            yield {}
            return
        started = self._monotonic()
        event = {"kind": kind, "status": "completed", **fields}
        self._notify({"kind": kind, "status": "started"})
        try:
            yield event
        except BaseException as error:
            event["status"] = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            if isinstance(error, QualityError):
                event["request_accepted"] = error.request_accepted
                event["http_status"] = error.status
            raise
        finally:
            event["elapsed_seconds"] = self._monotonic() - started
            self._notify(event)

    async def _send_request(self, body: JsonObject) -> HttpResponse:
        with self._observed("sol_http", request_accepted=None) as observation:
            response = await self._client.request(
                "POST", "/openai/v1/responses", body, api_version=None,
            )
            accepted = True if 200 <= response.status < 300 else (
                False if 400 <= response.status < 500 and response.status != 408 else None
            )
            observation.update(
                http_status=response.status, request_accepted=accepted,
                status="accepted" if accepted else "rejected" if accepted is False else "unknown",
            )
        if self.observer is not None:
            try:
                value = response.object()
            except QualityError:
                value = {}
            usage = value.get("usage")
            usage = usage if isinstance(usage, Mapping) else {}
            observed = {
                field: value if type(value := usage.get(field)) is int and 0 <= value < 2**63 else None
                for field in ("input_tokens", "output_tokens")
            }
            known = sum(value is not None for value in observed.values())
            self._notify({
                "kind": "sol_usage", "status": "observed", "http_status": response.status,
                "usage_status": "known" if known == 2 else "partial" if known else "unknown",
                **observed,
            })
        return response

    def _throttled(self, response: HttpResponse) -> None:
        until = self._monotonic() + _retry_delay(response, self._wall_clock())
        if self._cooldown_response is None or until >= self._cooldown_until:
            self._cooldown_until = until
            self._cooldown_response = response

    def _wait_error(self, reason: str, sent: int) -> SolResponseError:
        response = self._cooldown_response
        assert response is not None
        return SolResponseError(
            "sol_rate_limit_wait_exhausted", _response_detail(response),
            status=429, request_accepted=False,
            private_detail={
                "rate_limit": {"headers": _rate_headers(response)},
                "wait_reason": reason, "attempts_sent": sent, "shared_cooldown": True,
            },
        )

    async def _wait_for_cooldown(self, deadline: float, sent: int) -> None:
        # Already-in-flight requests may extend the shared deadline while we sleep.
        # Cap rechecks as well as elapsed time; a broken injected sleep must not spin.
        for _ in range(8):
            now = self._monotonic()
            if now > deadline:
                raise self._wait_error("total_wait_budget", sent)
            delay = self._cooldown_until - now
            if delay <= 0:
                return
            if delay > _MAX_RETRY_WAIT_SECONDS:
                raise self._wait_error("server_wait_exceeds_limit", sent)
            if delay > deadline - now:
                raise self._wait_error("total_wait_budget", sent)
            with self._observed("sol_cooldown", requested_seconds=delay):
                await self.sleep(delay)
            if self._monotonic() <= now:
                raise self._wait_error("clock_did_not_advance", sent)
        raise self._wait_error("cooldown_extension_limit", sent)

    async def _request(self, body: JsonObject) -> HttpResponse:
        deadline = self._monotonic() + _MAX_TOTAL_WAIT_SECONDS
        sent = 0
        response = None
        if self._cooldown_response is None:
            response = await self._send_request(body)
            sent = 1
            if response.status != 429:
                return response
            self._throttled(response)
        if sent == self.attempts:
            assert response is not None
            return response

        # Healthy traffic stays concurrent. Once throttled, FIFO recovery owns its
        # retries until completion so fresh calls cannot take the retry window.
        self._recovery_calls += 1
        acquired = False
        try:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise self._wait_error("total_wait_budget", sent)
            try:
                with self._observed("sol_recovery_queue"):
                    await asyncio.wait_for(self._recovery_gate.acquire(), timeout=remaining)
            except TimeoutError:
                raise self._wait_error("queue_wait_budget", sent) from None
            acquired = True
            while sent < self.attempts:
                await self._wait_for_cooldown(deadline, sent)
                response = await self._send_request(body)
                sent += 1
                if response.status != 429:
                    return response
                self._throttled(response)
            assert response is not None
            return response
        finally:
            if acquired:
                self._recovery_gate.release()
            self._recovery_calls -= 1
            if self._recovery_calls == 0 and self._monotonic() >= self._cooldown_until:
                self._cooldown_response = None

    async def complete_json(
        self,
        *,
        instructions: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> JsonObject:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            raise QualityError("sol_schema_invalid", request_accepted=False) from None
        body = {
            "model": self.deployment,
            "instructions": instructions,
            "input": encode(payload).decode("utf-8"),
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "assessment",
                    "strict": True,
                    "schema": _wire_schema(schema),
                }
            },
        }
        response = await self._request(body)
        invalid_json = False
        try:
            value = response.object()
        except QualityError:
            value = {"raw_body": response.body.decode("utf-8", errors="replace")}
            invalid_json = True
        try:
            check_status(response, {200})
        except QualityError as error:
            raise SolResponseError(
                "sol_http_error",
                value,
                status=response.status,
                request_accepted=error.request_accepted,
                retryable=error.retryable,
                private_detail={"rate_limit": {"headers": _rate_headers(response)}},
            ) from None
        if invalid_json:
            raise SolResponseError(
                "sol_response_invalid_json", value, status=response.status
            )
        status = value.get("status")
        if status == "incomplete":
            raise SolResponseError(
                "sol_response_incomplete", value, status=response.status
            )
        if status != "completed" or value.get("error"):
            raise SolResponseError("sol_response_failed", value, status=response.status)
        output = value.get("output")
        if not isinstance(output, list):
            raise SolResponseError("sol_output_missing", value, status=response.status)
        texts = []
        for item in output:
            if not isinstance(item, dict):
                raise SolResponseError(
                    "sol_output_invalid", value, status=response.status
                )
            if item.get("type") == "refusal":
                raise SolResponseError("sol_refused", value, status=response.status)
            if item.get("type") != "message":
                if item.get("type") == "reasoning":
                    continue
                raise SolResponseError(
                    "sol_output_invalid", value, status=response.status
                )
            content = item.get("content")
            if not isinstance(content, list):
                raise SolResponseError(
                    "sol_output_invalid", value, status=response.status
                )
            for part in content:
                if not isinstance(part, dict):
                    raise SolResponseError(
                        "sol_output_invalid", value, status=response.status
                    )
                if part.get("type") == "refusal" or part.get("refusal"):
                    raise SolResponseError("sol_refused", value, status=response.status)
                if part.get("type") != "output_text" or not isinstance(
                    part.get("text"), str
                ):
                    raise SolResponseError(
                        "sol_output_invalid", value, status=response.status
                    )
                texts.append(part["text"])
        try:
            result = json.loads(
                "".join(texts),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(result, dict):
                raise ValueError
            Draft202012Validator(
                schema, format_checker=Draft202012Validator.FORMAT_CHECKER
            ).validate(result)
        except (ValueError, ValidationError):
            raise SolResponseError(
                "sol_output_schema_invalid", value, status=response.status
            ) from None
        return result
