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

# 飞书回调的验签凭据同理：不置空的话，开发机 .env 里配好的
# FEISHU_VERIFICATION_TOKEN 会让 /feishu/event 在测试里要求 body 携带 token，
# 于是 5 条路由用例（构造的是不带 token 的 v2 事件）会 401 —— 这正是它们此前
# 被 skip 掉的真实原因之一（理由写的是"需要真实环境"，其实是被环境**污染**）。
# 置空 = 未配置；再显式开 insecure，让这两条路由在测试里可达（策略见
# app/middleware/auth.py，与 /webhook、/dashboard 共用同一判定）。
# 验签本身的行为由 test_feishu_signature.py 直接断言，不走 env。
for name in ("FEISHU_VERIFICATION_TOKEN", "FEISHU_ENCRYPT_KEY"):
    os.environ[name] = ""
os.environ["GATEWAY_ALLOW_INSECURE"] = "1"

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
