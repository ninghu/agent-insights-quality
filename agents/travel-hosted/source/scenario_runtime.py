from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any


class ScenarioRuntime:
    """Bounded interpreter for adapter-supplied synthetic scenario configuration."""

    def __init__(self) -> None:
        raw = os.environ.get("AIQ_SCENARIO_CONFIGURATION", "")
        if not raw:
            self._operations: tuple[dict[str, Any], ...] = ()
            self.instructions = ""
            return
        if len(raw.encode("ascii")) > 8192:
            raise RuntimeError("Scenario configuration exceeds the reviewed bound.")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "phase", "version_key", "operations"}
            or value["schema_version"] != "1.0.0"
            or not isinstance(value["operations"], list)
        ):
            raise RuntimeError("Scenario configuration is invalid.")
        self._operations = tuple(value["operations"])
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
        if self._has("tool_router", "replace_route"):
            return "Synthetic incompatible tool route selected."
        changed = dict(arguments)
        for operation in self._operations:
            if operation["target"] != "tool_arguments":
                continue
            if operation["action"] == "remove_field":
                changed.pop(str(operation["value"]), None)
            elif operation["action"] == "replace_value":
                replacement = operation["value"]
                changed[str(replacement["field"])] = replacement["value"]
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
