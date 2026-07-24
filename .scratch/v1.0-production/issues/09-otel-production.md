# OTel 生产配置

> wayfinder:task
> status: open

## Question

配置实际的 OTLP endpoint (Jaeger/Grafana)，关闭调试日志。

## Context

- Sprint 3 已实现 tracing 模块 (app/observability/tracing.py)
- 当前默认连 localhost:4317，无效时输出大量 Transient error 日志
- 需要实际的 OTLP Collector

## Resolution

<!-- 解决后填写 -->


## Resolution

**Done**: OTel now silent when no endpoint configured
- Default: OTLP disabled (no noisy Transient error logs)
- Enable: set OTEL_EXPORTER_OTLP_ENDPOINT=http://your-collector:4317
- Config: app/observability/tracing.py (TraceConfig.otlp_endpoint)
- Startup: app/main.py reads env var on boot
- OTLP fallback: ConsoleSpanExporter when endpoint is unreachable
