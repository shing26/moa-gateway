# ADR-007: OTel Tracing Production Config

**Status**: Accepted
**Date**: 2026-07-25

> **现状更正（2026-09-23）**：本 ADR 的两条承诺与实现**不符**，实测如下。
> ① "Enable via `OTEL_EXPORTER_OTLP_ENDPOINT`"：`opentelemetry-exporter-otlp` **未声明依赖**
> （`pyproject.toml` 只有 `opentelemetry-api` + `-sdk`，`uv.lock` 里出现 0 次），所以
> `tracing.py:29` 的 import 必然抛 ImportError → 被捕获 → **永远落到 `ConsoleSpanExporter`**：
> 设了 endpoint 也拿不到 OTLP。② "Fallback when endpoint is unreachable"：回退只在
> **构造期异常**触发，而 gRPC exporter 构造时不建连——collector 不可达只会在每次导出时报错。
> 另：全仓库只有 **2 个 span**（webhook 与 code-review 入口），pipeline / agent / LLM 均无
> span，也**无 context propagation**，因此 OTel 的 trace_id 与审计自建的 trace_id 是两套
> 互不关联的 ID。
>
> **结论：OTel 当前是"预留接口"，不是"链路"**（README 与 ADR-012 已按此降级表述）。
> 真接线需要：补 exporter 依赖 + 在 pipeline/agent/LLM 埋点 + propagation + 一个 collector
> 才能验收——**collector 本地 Docker 就能跑，所以这一项不是被外部条件卡住**，只是标准管道工作。

## Context

OTel was initialized with hardcoded localhost:4317 endpoint,
causing noisy Transient error logs when no collector is running.

## Decision

- Default: OTLP disabled (empty endpoint)
- Enable via OTEL_EXPORTER_OTLP_ENDPOINT env var
- Fallback to ConsoleSpanExporter when endpoint is unreachable
- Removed unused span() async context manager

## Consequences

Clean logs by default. Tracing enabled on demand with one env var.
