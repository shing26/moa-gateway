# 部署拓扑

> wayfinder:task
> status: resolved

## Decision

当前采用 **Windows 本地部署** 方案：

| 组件 | 方式 | 端口 |
|------|------|------|
| MoA Gateway | Python uvicorn (本地) | :8080 |
| Redis | Windows 二进制 (C:\ProgramData\redis) | :6379 |
| OmniRoute | npm 安装 (本地) | :20128 |
| 公网入口 | Cloudflare Quick Tunnel | trycloudflare.com |
| LLM 调用 | 通过 OmniRoute 路由 | auto/best-chat |

## 一键启动

start_moa.bat 自动按依赖顺序启动全部组件：
1. Redis → 2. Gateway → 3. Cloudflare Tunnel

## 未来可选项

- Docker Desktop 可用时切换为 docker-compose.dev.yml
- 固定域名用 Cloudflare Named Tunnel 替代 Quick Tunnel
