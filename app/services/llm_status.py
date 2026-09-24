"""LLM 配置的**展示**快照（ops 面板用）。

``LLM_*`` 的**客户端**唯一读取点是 ``LLMConfig.from_env``（app/agents/provider.py，
按前缀动态读 env）。但 ops 面板是**运行时可写**的——``POST /dashboard/api/ops/config``
直接写 env，展示面必须读活 env 才能反映运行时改动（test_dashboard_routes 钉住这个
语义）。此前 dashboard.py 与 dashboard_html.py **各自**读一套、各带一套默认值（model /
base_url 默认空串），与工厂的默认（gpt-4o-mini / api.openai.com）不一致——未配置时
面板报"未设置"，而客户端其实在用默认模型。

所以展示统一走这里这一个函数：读写面共享同一份默认值，配置守卫
（test_config_consistency）也把本文件列为该概念的唯一放行读者之一。
"""

from __future__ import annotations

import os
from typing import Any


def llm_snapshot() -> dict[str, Any]:
    return {
        "provider": os.environ.get("LLM_PROVIDER", "direct"),
        "model": os.environ.get("LLM_MODEL", ""),
        "base_url": os.environ.get("LLM_BASE_URL", ""),
        "api_key_set": bool(os.environ.get("LLM_API_KEY", "")),
    }


__all__ = ["llm_snapshot"]
