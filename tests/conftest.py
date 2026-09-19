"""全局测试环境配置。

必须在任何测试模块 import ``app.main`` **之前**注入鉴权密钥：``AuthMiddleware``
在 ``app.main`` 导入时就把令牌固化进中间件实例（app/main.py 装配段），
之后再改环境变量对中间件无效。pytest 会先加载本文件，因此这里是唯一可靠时机。

用 ``setdefault`` 而非直接赋值：本机已配置真实密钥时不覆盖，
测试凭据统一由 ``tests/support.py`` 从环境变量回读，二者不会脱节。
"""

from __future__ import annotations

import os

DEFAULT_GATEWAY_TOKEN = "test-gateway-token"  # nosec B105
DEFAULT_DASHBOARD_PASSWORD = "test-dashboard-pw"  # nosec B105

os.environ.setdefault("WEBHOOK_AUTH_TOKEN", DEFAULT_GATEWAY_TOKEN)
os.environ.setdefault("DASHBOARD_PASSWORD", DEFAULT_DASHBOARD_PASSWORD)

# Unit tests must never inherit a developer's real database configuration via
# app.config.load_dotenv().  Empty values take precedence over .env because
# python-dotenv does not overwrite pre-existing environment variables.
for name in (
    "VECTOR_DB_DSN",
    "CODE_REVIEW_DATABASE_URL",
    "DATABASE_URL",
    "POSTGRES_URL",
):
    os.environ[name] = ""

# GATEWAY_PORT/APP_PORT 同理：开发机 .env 若写了非法端口，config.validate()
# 会在 import 期 raise，把整个测试进程炸掉。置空 = 未配置 = 默认 8081。
for name in ("GATEWAY_PORT", "APP_PORT"):
    os.environ[name] = ""

# 同理，单测不能继承开发者 .env 里的真实 LLM 凭据：意图路由的
# router_llm 会在测试期间真的发起外网请求，超时取消后还会触发 litellm
# 的 “coroutine was never awaited” 告警，让结果依赖网络。需要 LLM 配置
# 的用例各自用 monkeypatch 显式注入（见 test_intent_router_wiring.py）。
for name in (
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_FALLBACK_MODELS",
    "ROUTER_LLM_MODEL",
    "ROUTER_LLM_API_KEY",
    "ROUTER_LLM_BASE_URL",
    "MICRO_LLM_MODEL",
    "MICRO_LLM_API_KEY",
    "MICRO_LLM_BASE_URL",
    "OPENAI_API_KEY",
    "OMNIROUTE_API_KEY",
):
    os.environ[name] = ""
