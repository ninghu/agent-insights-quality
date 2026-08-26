from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_observability(service_name: str) -> None:
    collector = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not collector:
        return
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=collector))
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        current.add_span_processor(processor)
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
