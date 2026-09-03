from __future__ import annotations

import os

from langchain_azure_ai.callbacks.tracers import enable_auto_tracing
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_observability(service_name: str, service_version: str) -> None:
    collector = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not collector:
        return
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=collector))
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        current.add_span_processor(processor)
        enable_auto_tracing(
            auto_configure_azure_monitor=False,
            trace_all_langgraph_nodes=False,
        )
        return
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.version": service_version,
                "gen_ai.agent.name": service_name,
                "gen_ai.agent.version": service_version,
            }
        )
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    enable_auto_tracing(
        auto_configure_azure_monitor=False,
        trace_all_langgraph_nodes=False,
    )
