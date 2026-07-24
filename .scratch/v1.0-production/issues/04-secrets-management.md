# 密钥管理

> wayfinder:research
> status: open

## Question

API Key / 数据库密码等敏感配置存在哪里？环境变量 / .env 文件 / Vault？

## Context

- 当前使用 PowerShell 的 $env: 设置
- 生产环境不应依赖手动设置
- Docker 可以用 .env 文件或 secrets

## Options

A) .env 文件 + .gitignore
B) Docker Secrets
C) HashiCorp Vault
D) 环境变量 + 部署脚本设置

## Resolution

<!-- 解决后填写 -->


## Resolution

**Decision**: Option A (.env file)
- Created: .env.template (commit as template)
- User copies to .env (in .gitignore, not committed)
- Docker Compose auto-reads .env, no config change needed
- .env.template covers: OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL, REDIS_URL, REDIS_SENTINEL_*, FEISHU_*
