from __future__ import annotations

import logging
import os
import re
from typing import Any

from app.agents.provider import LLMClient, LLMConfig

logger = logging.getLogger("moa.evals.judge")


async def score(input_text: str, output_text: str, criteria: str) -> float:
    config = LLMConfig(
        api_key=os.getenv("JUDGE_API_KEY", os.getenv("OPENAI_API_KEY", "")),
        base_url=os.getenv("JUDGE_BASE_URL", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")),
        model=os.getenv("JUDGE_MODEL", "gpt-4o-mini"),
    )
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
