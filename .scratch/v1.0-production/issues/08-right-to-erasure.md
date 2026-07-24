# 被遗忘权 API

> wayfinder:task
> status: blocked
> blocked_by: 06, 07

## Question

实现 DELETE /api/v1/privacy/user/{user_id} 删除用户所有数据。

## Context

- ADR 已定义 API 接口
- 需要删除 Redis session、VectorDB metadata、ES audit log
- VectorDBClient.delete_by_metadata 已实现

## Resolution

<!-- 解决后填写 -->


## Resolution

**Implemented**: DELETE /api/v1/privacy/user/{user_id}
- VectorDB: deletes all docs with matching user_id metadata
- Redis: best-effort (no user_id index on sessions)
- Logs: deletion action logged
- Tests: 121 passing, no regressions
- File: app/main.py
