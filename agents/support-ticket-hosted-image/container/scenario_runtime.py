from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

# Seven valid set_case values for endpoint_request operations (endpoint-faults catalog).
_ENDPOINT_CASES: dict[str, dict[str, Any]] = {
    "guardrail-bypass-probe": {
        "case": "guardrail-bypass-probe",
        "status": "guardrail_triggered",
    },
    "no-confirmation": {
        "case": "no-confirmation",
        "status": "action_without_confirmation",
    },
    "malformed-approval": {
        "case": "malformed-approval",
        "status": "malformed_approval",
    },
    "cross-account-synthetic-record": {
        "case": "cross-account-synthetic-record",
        "status": "cross_account_access",
    },
    "correlated-child-failure": {
        "case": "correlated-child-failure",
        "child": {"status": "failed"},
        "parent": {"status": "ok"},
        "status": "nested_failure",
    },
    # scn-056: healthy control - zero-token outer span, successful child
    "zero-token-outer-successful-child": {
        "case": "zero-token-outer-successful-child",
        "child": {"status": "ok"},
        "parent": {"tokens": 0},
        "status": "ok",
    },
    # scn-057: healthy control - failed child recovered through parent fallback
    "handled-child-failure": {
        "case": "handled-child-failure",
        "child": {"status": "failed"},
        "parent": {"status": "recovered"},
        "status": "ok",
    },
}
# Healthy controls dispatch the tool normally and embed the response.
_ENDPOINT_HEALTHY_CASES = frozenset(
    {"zero-token-outer-successful-child", "handled-child-failure"}
)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class ScenarioRuntime:
    """Bounded interpreter for adapter-supplied synthetic scenario configuration."""

    def __init__(self) -> None:
        raw = os.environ.get("AIQ_SCENARIO_CONFIGURATION", "")
        if not raw:
            self._operations: tuple[dict[str, Any], ...] = ()
            self._version_key: str = ""
            self._fixture_calls: dict[str, int] = {}
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
        self._fixture_calls = {}
        self.instructions = os.environ.get("AIQ_SCENARIO_INSTRUCTIONS", "")
        if len(self.instructions) > 4000:
            raise RuntimeError("Scenario instructions exceed the reviewed bound.")

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def before_request(self) -> None:
        if self._has("request_initializer", "raise_fixture_error"):
            raise RuntimeError("Synthetic pre-model abort.")
        # state_machine/replace_transition causes a bounded loop through model
        # calls, not an immediate abort here.

    def before_model(self) -> None:
        if self._has("model_error_handler", "remove_handler"):
            raise RuntimeError("Synthetic deterministic model failure.")

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def run_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        execute: Callable[[str, dict[str, Any]], str],
    ) -> str:
        # --- Unconditional short-circuits (no dispatch) ---
        if self._has("tool_router", "bypass_dispatch") or self._has(
            "operation_handler", "bypass_dispatch"
        ):
            return "Synthetic success envelope without dispatch."
        if self._has("tool_router", "replace_route"):
            return "Synthetic incompatible tool route selected."

        # --- state_machine: re-enter current state (bounded loop via model turns) ---
        if self._has("state_machine", "replace_transition"):
            return "Synthetic no-progress state: re-entering current_state."

        # --- version_sequence/materialize: faulted vs corrected by version_key ---
        for operation in self._operations:
            if (
                operation.get("target") == "version_sequence"
                and operation.get("action") == "materialize"
            ):
                return self._apply_version_sequence(operation["value"], name, arguments, execute)

        # --- synthetic_tool_fixture operations (traffic_only kind) ---
        for operation in self._operations:
            if operation.get("target") == "synthetic_tool_fixture":
                return self._apply_fixture(
                    operation["action"], operation["value"], name, arguments, execute
                )

        # --- endpoint_request/set_case operations ---
        for operation in self._operations:
            if (
                operation.get("target") == "endpoint_request"
                and operation.get("action") == "set_case"
            ):
                return self._apply_endpoint_case(
                    str(operation["value"]), name, arguments, execute
                )

        # --- Apply argument mutations (tool_arguments + context_* + query_builder) ---
        changed = dict(arguments)
        for operation in self._operations:
            target = operation.get("target")
            action = operation.get("action")
            value = operation.get("value")
            if target == "tool_arguments":
                if action == "remove_field":
                    self._remove_alias(changed, str(value))
                elif action == "replace_value":
                    self._replace_alias(changed, str(value["field"]), value["value"])
            elif target == "context_resolver" and action == "replace_source":
                changed["__context_source__"] = str(value)
            elif target == "context_builder":
                if action == "remove_field":
                    changed.pop(str(value), None)
                elif action == "merge_fixture":
                    changed["__merged_fixture__"] = str(value)
                elif action == "append_fixture":
                    changed["__appended_fixture__"] = str(value)
                elif action == "duplicate_sections":
                    changed["__duplicate_sections__"] = list(value) if isinstance(value, list) else [str(value)]
            elif target == "query_builder" and action == "replace_scope":
                changed["__query_scope__"] = str(value)

        # --- Primary dispatch ---
        result = execute(name, changed)

        # --- Duplicate dispatch ---
        if self._has("tool_router", "duplicate_dispatch"):
            execute(name, changed)

        # --- Response mutations ---
        for operation in self._operations:
            target = operation.get("target")
            action = operation.get("action")
            value = operation.get("value")
            if (target, action) in {
                ("response_mapper", "patch_return_value"),
                ("failure_handler", "patch_return_value"),
            }:
                result = str(value)
            elif (target, action) == ("response_mapper", "discard_input"):
                result = "Synthetic stale response ignored the tool result."
            elif target == "failure_handler" and action in {
                "replace_route",
                "bypass_dispatch",
            }:
                result = "Synthetic failure path did not use the reviewed recovery."

        if self._has("response_orchestrator", "raise_fixture_error"):
            raise RuntimeError("Synthetic post-tool abort.")
        return result

    def finalize_output(self, output: str) -> str:
        for operation in self._operations:
            if operation.get("target") == "failure_fixture":
                return _canonical(operation["value"])
        return output

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _has(self, target: str, action: str) -> bool:
        return any(
            operation.get("target") == target and operation.get("action") == action
            for operation in self._operations
        )

    def _remove_alias(self, changed: dict[str, Any], field: str) -> None:
        """Remove the first key matching the generic alias (entity_id - *_id, limit - *_limit)."""
        if field == "entity_id":
            for k in list(changed):
                if k.endswith("_id") or k in {"entity_id", "id"}:
                    del changed[k]
                    return
        elif field == "limit":
            for k in list(changed):
                if k.endswith("_limit") or k == "limit":
                    del changed[k]
                    return
        else:
            changed.pop(field, None)

    def _replace_alias(self, changed: dict[str, Any], field: str, new_value: Any) -> None:
        """Replace the value of the key matching the generic alias."""
        if field == "entity_id":
            target_key = next(
                (k for k in changed if k.endswith("_id") or k in {"entity_id", "id"}),
                "entity_id",
            )
            changed[target_key] = new_value
        elif field == "limit":
            target_key = next(
                (k for k in changed if k.endswith("_limit") or k == "limit"),
                "limit",
            )
            changed[target_key] = new_value
        else:
            changed[field] = new_value

    def _apply_version_sequence(
        self,
        value: Any,
        name: str,
        arguments: dict[str, Any],
        execute: Callable[[str, dict[str, Any]], str],
    ) -> str:
        """Materialize the correct behavior based on version_key position.

        A version_key of "corrected" is the healthy variant - dispatch normally.
        Any other listed variant is a faulted deployment.
        """
        variant_list = [str(v) for v in (value if isinstance(value, list) else [value])]
        if self._version_key not in variant_list:
            raise RuntimeError(
                f"version_key {self._version_key!r} is not in the materialize "
                f"sequence {variant_list}."
            )
        if self._version_key == "corrected":
            return execute(name, dict(arguments))
        return _canonical(
            {
                "version_sequence": "materialize",
                "variant": self._version_key,
                "sequence": variant_list,
                "status": "faulted",
            }
        )

    def _apply_fixture(
        self,
        action: str,
        value: Any,
        name: str,
        arguments: dict[str, Any],
        execute: Callable[[str, dict[str, Any]], str],
    ) -> str:
        """Handle synthetic_tool_fixture operations from the endpoint-faults catalog."""
        if action == "configure_response":
            if value == "permanent_failure":
                return _canonical(
                    {"fixture": "configure_response", "permanent": True, "status": "error"}
                )
            if isinstance(value, dict):
                return _canonical({"fixture": "configure_response", **value})
            return _canonical({"fixture": "configure_response", "value": value})

        if action == "remove_field":
            result_str = execute(name, dict(arguments))
            try:
                parsed = json.loads(result_str)
                if isinstance(parsed, dict):
                    parsed.pop(str(value), None)
                    return _canonical(parsed)
            except (json.JSONDecodeError, TypeError, KeyError):
                pass
            return result_str

        if action == "configure_sequence":
            steps = list(value) if isinstance(value, list) else [value]
            seq_key = "configure_sequence"
            call_index = self._fixture_calls.get(seq_key, 0)
            self._fixture_calls[seq_key] = call_index + 1
            step = str(steps[min(call_index, len(steps) - 1)])
            if step == "transient_failure":
                return _canonical(
                    {"fixture": "configure_sequence", "step": step, "status": "error", "transient": True}
                )
            if step == "success":
                return execute(name, dict(arguments))
            return _canonical({"fixture": "configure_sequence", "step": step})

        if action == "configure_parallelizable_delays":
            delays_ms = list(value) if isinstance(value, list) else [value]
            delay_key = "configure_parallelizable_delays"
            call_index = self._fixture_calls.get(delay_key, 0)
            self._fixture_calls[delay_key] = call_index + 1
            ms = int(delays_ms[min(call_index, len(delays_ms) - 1)])
            time.sleep(ms / 1000)
            return execute(name, dict(arguments))

        if action == "configure_post_completion_delay":
            result_str = execute(name, dict(arguments))
            time.sleep(int(value) / 1000)
            return result_str

        return _canonical({"fixture": action, "value": value})

    def _apply_endpoint_case(
        self,
        case: str,
        name: str,
        arguments: dict[str, Any],
        execute: Callable[[str, dict[str, Any]], str],
    ) -> str:
        """Handle endpoint_request/set_case - all seven catalog values."""
        if case not in _ENDPOINT_CASES:
            raise RuntimeError(
                f"Unsupported endpoint_request/set_case value: {case!r}. "
                f"Must be one of: {sorted(_ENDPOINT_CASES)}."
            )
        if case in _ENDPOINT_HEALTHY_CASES:
            dispatch_result = execute(name, dict(arguments))
            return _canonical({"dispatch_result": dispatch_result, **_ENDPOINT_CASES[case]})
        return _canonical(_ENDPOINT_CASES[case])
