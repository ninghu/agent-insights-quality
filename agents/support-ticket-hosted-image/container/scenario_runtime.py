from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

# Stable per-instance state for version_sequence fault semantics keyed by version_key.
# Each ScenarioRuntime instance maintains its own counter so parallel scenarios do not
# interfere; the version_key from the configuration schema is the stable identity.


class ScenarioRuntime:
    """Bounded interpreter for adapter-supplied synthetic scenario configuration."""

    def __init__(self) -> None:
        raw = os.environ.get("AIQ_SCENARIO_CONFIGURATION", "")
        if not raw:
            self._operations: tuple[dict[str, Any], ...] = ()
            self._version_key: str = ""
            self._version_sequence_calls: int = 0
            self.instructions = ""
            return
        if len(raw.encode("ascii")) > 8192:
            raise RuntimeError("Scenario configuration exceeds the reviewed bound.")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "version_key", "operations"}
            or value["schema_version"] != "1.0.0"
            or not isinstance(value["version_key"], str)
            or not value["version_key"]
            or not isinstance(value["operations"], list)
        ):
            raise RuntimeError("Scenario configuration is invalid.")
        self._operations = tuple(value["operations"])
        self._version_key = value["version_key"]
        self._version_sequence_calls = 0
        self.instructions = os.environ.get("AIQ_SCENARIO_INSTRUCTIONS", "")
        if len(self.instructions) > 4000:
            raise RuntimeError("Scenario instructions exceed the reviewed bound.")

    def before_request(self) -> None:
        if self._has("request_initializer", "raise_fixture_error"):
            raise RuntimeError("Synthetic pre-model abort.")
        if self._has("state_machine", "replace_transition"):
            raise RuntimeError("Synthetic bounded no-progress loop.")

    def before_model(self) -> None:
        if self._has("model_error_handler", "remove_handler"):
            raise RuntimeError("Synthetic deterministic model failure.")

    def run_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        execute: Callable[[str, dict[str, Any]], str],
    ) -> str:
        if self._has("tool_router", "bypass_dispatch") or self._has(
            "operation_handler", "bypass_dispatch"
        ):
            return "Synthetic success envelope without dispatch."
        if self._has("traffic_only", "enforce"):
            return "Synthetic read-only traffic: no dispatch performed."
        if self._has("tool_router", "replace_route"):
            return "Synthetic incompatible tool route selected."

        # Apply argument mutations from tool_arguments and source_patch operations.
        changed = dict(arguments)
        for operation in self._operations:
            target = operation["target"]
            action = operation["action"]
            value = operation["value"]
            if target == "tool_arguments":
                if action == "remove_field":
                    changed.pop(str(value), None)
                elif action == "replace_value":
                    changed[str(value["field"])] = value["value"]
            elif target == "source_patch":
                # source_patch: generic entity_id/limit aliases resolved from value dict.
                if action == "remove_field":
                    field = str(value)
                    # Support entity_id and limit as generic alias keys.
                    if field == "entity_id":
                        for alias in list(changed):
                            if alias in {"entity_id", "id", "entity"}:
                                changed.pop(alias, None)
                    elif field == "limit":
                        for alias in list(changed):
                            if alias in {"limit", "max", "count"}:
                                changed.pop(alias, None)
                    else:
                        changed.pop(field, None)
                elif action == "replace_value":
                    field = str(value["field"])
                    replacement = value["value"]
                    # Resolve entity_id / limit aliases.
                    if field == "entity_id":
                        resolved = next(
                            (k for k in changed if k in {"entity_id", "id", "entity"}),
                            "entity_id",
                        )
                        changed[resolved] = replacement
                    elif field == "limit":
                        resolved = next(
                            (k for k in changed if k in {"limit", "max", "count"}),
                            "limit",
                        )
                        changed[resolved] = replacement
                    else:
                        changed[field] = replacement

        # Apply context mutations before dispatch.
        context_override: str | None = None
        for operation in self._operations:
            target = operation["target"]
            action = operation["action"]
            value = operation["value"]
            if target == "context_resolver" and action == "replace_context":
                context_override = str(value)
            elif target == "context_builder" and action == "append_context":
                suffix = str(value)
                context_override = (
                    f"{context_override} {suffix}" if context_override else suffix
                )
            elif target == "context_query" and action == "mock_result":
                return json.dumps(
                    {"context_query_result": value},
                    sort_keys=True,
                    separators=(",", ":"),
                )

        # Apply pre-dispatch delays.
        if self._has("delay", "ms_120"):
            time.sleep(0.120)
        if self._has("delay", "ms_250"):
            time.sleep(0.250)

        # version_sequence: transient fault on first call, dispatch on second.
        if self._has("version_sequence", "transient_then_success"):
            self._version_sequence_calls += 1
            if self._version_sequence_calls == 1:
                raise RuntimeError(
                    f"Synthetic transient fault for version_key={self._version_key!r}."
                )

        # endpoint_request synthetic OTel parent/child behavior.
        if self._has("endpoint_request", "synthetic_otel_parent"):
            return json.dumps(
                {
                    "endpoint_request": "synthetic_otel_parent",
                    "traceparent": f"00-{'a' * 32}-{'b' * 16}-01",
                    "version_key": self._version_key,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        if self._has("endpoint_request", "synthetic_otel_child"):
            return json.dumps(
                {
                    "endpoint_request": "synthetic_otel_child",
                    "traceparent": f"00-{'a' * 32}-{'c' * 16}-01",
                    "version_key": self._version_key,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        # Healthy endpoint_request controls (056/057) dispatch normally.
        if self._has("endpoint_request", "healthy_056") or self._has(
            "endpoint_request", "healthy_057"
        ):
            result = execute(name, changed)
            return result

        if context_override is not None:
            changed["__context__"] = context_override

        result = execute(name, changed)
        if self._has("tool_router", "duplicate_dispatch"):
            execute(name, changed)

        for operation in self._operations:
            target = operation["target"]
            action = operation["action"]
            value = operation["value"]
            if (target, action) in {
                ("response_mapper", "patch_return_value"),
                ("failure_handler", "patch_return_value"),
            }:
                result = str(value)
            elif (target, action) == ("response_mapper", "discard_input"):
                result = "Synthetic stale response ignored the tool result."
            elif target == "failure_handler" and action in {"replace_route", "bypass_dispatch"}:
                result = "Synthetic failure path did not use the reviewed recovery."
            elif target == "synthetic_tool_fixture":
                result = json.dumps(
                    {"action": action, "value": value},
                    sort_keys=True,
                    separators=(",", ":"),
                )
        if self._has("response_orchestrator", "raise_fixture_error"):
            raise RuntimeError("Synthetic post-tool abort.")
        return result

    def finalize_output(self, output: str) -> str:
        for operation in self._operations:
            if operation["target"] == "failure_fixture":
                return json.dumps(operation["value"], sort_keys=True)
        return output

    def _has(self, target: str, action: str) -> bool:
        return any(
            operation.get("target") == target and operation.get("action") == action
            for operation in self._operations
        )
