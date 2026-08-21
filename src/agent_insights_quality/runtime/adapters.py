from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from typing import Any

from .config import RuntimeConfig
from .errors import RuntimeFailure
from .orchestrator import RuntimeHooks

_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
DEFAULT_RUNTIME_ADAPTER = "agent_insights_quality.live_adapter"
ALLOWED_RUNTIME_ADAPTERS = frozenset({DEFAULT_RUNTIME_ADAPTER})


class ValidationOnlyHooks:
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config

    def preflight(self, _plan: Any, *, dry_run: bool) -> Mapping[str, Any]:
        if not dry_run:
            raise RuntimeFailure("runtime_adapter_unavailable", "Validation-only hooks cannot execute.")
        return self._config.public_summary()

    @staticmethod
    def _unavailable(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        raise RuntimeFailure("runtime_adapter_unavailable", "A reviewed runtime adapter is required.")

    ensure_project = _unavailable
    deploy = _unavailable
    invoke = _unavailable
    wait_ingestion = _unavailable
    run_insights = _unavailable
    assemble_evidence = _unavailable

    @staticmethod
    def cancel(_work: Any) -> None:
        return None

    @staticmethod
    def finalize_failure(_failure: RuntimeFailure, _state: Mapping[str, Any]) -> None:
        return None


def load_runtime_hooks(config: RuntimeConfig, *, validation_only: bool = False) -> RuntimeHooks:
    name = config.adapter
    if validation_only and not name:
        return ValidationOnlyHooks(config)
    if (
        not name
        or not _MODULE.fullmatch(name)
        or name not in ALLOWED_RUNTIME_ADAPTERS
    ):
        raise RuntimeFailure(
            "runtime_adapter_unavailable",
            "AIQ_RUNTIME_ADAPTER must name an explicitly reviewed runtime adapter.",
        )
    try:
        module = importlib.import_module(name)
    except ImportError as error:
        raise RuntimeFailure(
            "runtime_adapter_unavailable",
            "The configured runtime adapter could not be imported.",
        ) from error
    factory = getattr(module, "create_runtime_hooks", None)
    if not callable(factory):
        raise RuntimeFailure(
            "runtime_adapter_unavailable",
            "Runtime adapter must export create_runtime_hooks(config).",
        )
    hooks: Any = factory(config)
    required = {
        "preflight",
        "ensure_project",
        "deploy",
        "invoke",
        "wait_ingestion",
        "run_insights",
        "assemble_evidence",
        "cancel",
        "finalize_failure",
    }
    if any(not callable(getattr(hooks, method, None)) for method in required):
        raise RuntimeFailure(
            "runtime_adapter_unavailable",
            "Runtime adapter does not implement every required hook.",
        )
    return hooks
