# 审计日志持久化

> wayfinder:task
> status: blocked
> blocked_by: 06

## Question

配置实际的日志存储 (ES/文件/其他)，确保 PIPL 要求的审计记录可查。

## Context

- Sprint 3 EsWriter + AsyncWal 已实现
- EsWriter 可以写 ES，失败时降级到 WAL
- 如果不用 ES，可以写本地文件

## Resolution

<!-- 解决后填写 -->


## Resolution

**Deferred**: ES integration is optional for v1.0 MVP
- AsyncWal already persists logs to local WAL (memory + disk)
- EsWriter can be configured later when ES is available
- Log cleanup: set LOG_RETENTION_DAYS env var (default 90)
