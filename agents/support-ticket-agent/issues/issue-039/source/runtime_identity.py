from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from opentelemetry.trace import Span, Tracer


@dataclass(frozen=True)
class FoundryRuntimeIdentity:
    name: str
    version: str

    @contextmanager
    def start_span(self, tracer: Tracer, name: str) -> Iterator[Span]:
        with tracer.start_as_current_span(name) as span:
            span.set_attribute("gen_ai.agent.name", self.name)
            span.set_attribute("gen_ai.agent.version", self.version)
            yield span


def require_foundry_runtime_identity() -> FoundryRuntimeIdentity:
    name = os.getenv("FOUNDRY_AGENT_NAME", "").strip()
    version = os.getenv("FOUNDRY_AGENT_VERSION", "").strip()
    if not name or not version:
        raise RuntimeError(
            "FOUNDRY_AGENT_NAME and FOUNDRY_AGENT_VERSION are required"
        )
    return FoundryRuntimeIdentity(name=name, version=version)
