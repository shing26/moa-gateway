# Redis Sentinel 上线

> wayfinder:task
> status: open
> blocking: 10

## Question

配置 Redis Sentinel 集群并验证故障转移，确保 MoA Gateway 能自动连接到新主节点。

## Context

- Sprint 4 已实现 Sentinel 连接代码 (app/redis_state/store.py)
- 当前 MemoryStateStore 作为降级备份
- 至少需要 3 个 Redis 节点 + 3 个 Sentinel 节点
- 首先在 Docker Compose 中配置测试环境

## Resolution

<!-- 解决后填写 -->
