from __future__ import annotations

import logging
import os
import re
from typing import Any

from app.agents.provider import LLMClient, LLMConfig

logger = logging.getLogger("moa.evals.judge")


async def score(input_text: str, output_text: str, criteria: str) -> float:
    config = _judge_config()
    prompt = (
        "你是评估器。根据给定标准为助手输出打分，只输出 0 到 1 之间的小数。\n"
        f"标准: {criteria}\n"
        f"用户输入: {input_text}\n"
        f"助手输出: {output_text[:4000]}\n"
        "分数:"
    )
    async with LLMClient(config) as client:
        content = await client.chat([{"role": "user", "content": prompt}])
    match = re.search(r"0(?:\.\d+)?|1(?:\.0+)?", content.strip())
    if match:
        return max(0.0, min(1.0, float(match.group(0))))
    return 0.0


def _judge_config() -> LLMConfig:
    """评测器用哪个模型。

    默认**复用项目的 ``LLM_*`` 配置**（本项目默认本地 Ollama）。此前这里自建一份
    ``LLMConfig``：base_url 取 ``OPENAI_BASE_URL``、model 却硬编码 ``gpt-4o-mini``——
    两个来源混用。local 形态下 ``OPENAI_BASE_URL`` 指向 Ollama，于是请求打到一个没有
    gpt-4o-mini 的端点，e2e 评测必然 404（2026-09-22 实测 ``model 'gpt-4o-mini' not found``）。
    这是 e2e 长期只跑 ``--offline`` 的原因之一：真跑一次就炸。

    设了 ``JUDGE_MODEL`` 时改用 ``JUDGE_*`` 全家（评测想用更强模型时显式指定，
    此时也需一并给 ``JUDGE_BASE_URL`` / ``JUDGE_API_KEY``）。
    """
    if os.getenv("JUDGE_MODEL"):
        return LLMConfig.from_env("JUDGE")
    return LLMConfig.from_env("LLM")
