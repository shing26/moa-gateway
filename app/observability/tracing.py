from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

logger = logging.getLogger("moa.otel")


@dataclass
class TraceConfig:
    service_name: str = "moa-gateway"
    otlp_endpoint: str = ""
    console_fallback: bool = True


def setup_tracing(config: TraceConfig | None = None) -> TracerProvider:
    cfg = config or TraceConfig()
    provider = TracerProvider(resource=Resource.create({"service.name": cfg.service_name}))

    if cfg.otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=cfg.otlp_endpoint, insecure=True)
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            logger.info("otel tracing enabled: %s -> %s", cfg.service_name, cfg.otlp_endpoint)
        except Exception as exc:
            logger.warning("otel otlp init failed: %s; falling back to console", exc)
            exporter = ConsoleSpanExporter()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        logger.info("otel tracing disabled (no endpoint configured)")
    trace.set_tracer_provider(provider)
    return provider
