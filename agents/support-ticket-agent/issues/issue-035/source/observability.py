from __future__ import annotations

import os

from opentelemetry import trace
from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_observability(service_name: str, service_version: str) -> None:
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    collector = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not connection_string and not collector:
        return
    exporter = (
        AzureMonitorTraceExporter(connection_string=connection_string)
        if connection_string
        else OTLPSpanExporter(endpoint=collector)
    )
    processor = BatchSpanProcessor(exporter)
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        current.add_span_processor(processor)
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
