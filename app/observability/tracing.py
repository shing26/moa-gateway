from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource

try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    _HAS_OTLP = True
except ImportError:
    OTLPSpanExporter = None  # type: ignore
    _HAS_OTLP = False

logger = logging.getLogger("moa.otel")


@dataclass(frozen=True)
class TraceConfig:
    service_name: str = "moa-gateway"
    otlp_endpoint: str = "http://localhost:4317"
    console_fallback: bool = True


def setup_tracing(config: TraceConfig | None = None) -> TracerProvider:
    cfg = config or TraceConfig()
    provider = TracerProvider(resource=Resource.create({"service.name": cfg.service_name}))
    if not _HAS_OTLP:
        logger.warning("opentelemetry-exporter-otlp-proto-grpc not installed; using console exporter")
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        exporter: Any = ConsoleSpanExporter()
    else:
        exporter = OTLPSpanExporter(endpoint=cfg.otlp_endpoint, insecure=True)  # type: ignore
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info("otel tracing initialized: %s -> %s", cfg.service_name, cfg.otlp_endpoint)
    return provider


@contextlib.asynccontextmanager
async def span(name: str, tracer: Any = None) -> Any:
    if tracer is None:
        tracer = trace.get_current_span()
    span = tracer.start_as_current_span(name) if hasattr(tracer, "start_as_current_span") else tracer.start_span(name)
    token = trace.use_span(span, end_on_exit=False)
    try:
        yield span
    finally:
        span.end()
        trace.reset_span(token)
