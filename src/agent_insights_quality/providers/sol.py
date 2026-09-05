from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from agent_insights_quality.contracts import Environment, JsonObject
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.transport import (
    AzureHttpTransport,
    JsonClient,
    Transport,
    check_status,
    encode,
)


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
    ) -> None:
        super().__init__(
            code, request_accepted=request_accepted, status=status, retryable=retryable
        )
        self.response = response


class AzureSol:
    def __init__(
        self,
        environment: Environment,
        *,
        transport: Transport | None = None,
        deployment: str = "sol-assessment",
        attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
        for attempt in range(self.attempts):
            response = await self._client.request(
                "POST", "/openai/v1/responses", body, api_version=None
            )
            # A received rate-limit rejection is safe to retry; an unknown POST is not.
            if response.status != 429 or attempt + 1 == self.attempts:
                break
            retry_after = response.header("Retry-After")
            try:
                delay = float(retry_after) if retry_after is not None else 2**attempt
            except (ValueError, OverflowError):
                delay = 2**attempt
            if not math.isfinite(delay) or delay < 0:
                delay = 2**attempt
            # Do not retry before the server's reset. Longer waits remain an
            # explicit rate-limit failure for checkpointed recovery.
            if delay > 300:
                break
            await self.sleep(delay)
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
