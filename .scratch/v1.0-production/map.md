# MoA Engine v1.0 生产化

> wayfinder:map

## Destination

生产级别的 MoA Gateway，符合 PIPL 合规要求，Redis Sentinel 高可用。本地运行，不接多渠道。

## Notes

- 仓库: <repo-root>
- 分支: feat/v0.5-sprint4
- 现有 121 个单元测试全绿
- 已实现: 线程内存降级、异步 WAL、特征标志、提示词注册、RBAC Guard、OTel Tracing

## Decisions so far

<!-- 每解决一个 ticket 就在这里追加一行 -->

## Not yet specified

- 域名 / SSL / CDN 配置细节
- 从本地部署到生产环境的迁移策略
- 计费与成本控制

## Out of scope

- 多渠道接入 (Discord/Slack) — 不在 v1.0 范围
- Embedding 模型热切换
- 混沌工程集成测试
- K8s HPA + 多 Region 部署

## Frontier

以下是当前可以解决的 tickets，按 blocking 关系排列。首先解决 frontier 上的“部署拓扑”。

| # | Ticket | 类型 | 状态 | Blocked by |
|---|--------|------|------|-----------|
| 1 | 部署拓扑 | Grilling | ✔️ Frontier | - |
| 2 | 域名/SSL/CDN | Research | ⏳ Blocked | 1 |
| 3 | CI/CD 流水线 | Grilling | ⏳ Blocked | 1, 2 |
| 4 | 密钥管理 | Research | ✔️ Frontier | - |
| 5 | Redis Sentinel 上线 | Task | ✔️ Frontier | - |
| 6 | PIPL 数据本地化 | Grilling | ✔️ Frontier | - |
| 7 | 审计日志持久化 | Task | ⏳ Blocked | 6 |
| 8 | 被遗忘权 API | Task | ⏳ Blocked | 6, 7 |
| 9 | OTel 生产配置 | Task | ✔️ Frontier | - |
| 10 | 优雅关闭 | Research | ⏳ Blocked | 5 |

## Frontier queries

- open, unblocked, unassigned tickets
- 当前 frontier: #1 部署拓扑, #4 密钥管理, #5 Redis Sentinel, #6 PIPL, #9 OTel
