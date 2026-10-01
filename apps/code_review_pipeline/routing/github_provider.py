"""GitHub 客户端的**唯一**选型点（D5）。

之前 ``CodeReviewPipeline.from_env`` 直接 ``GitHubClient.from_env()``，于是
"用不用真 GitHub"这件事没有单独的表达位置——要么改代码，要么配 token。加了离线
通道后如果仍各自判断，就又是"同一语义两处各写一遍"（本仓库已因此栽了六次）。
所以选型只在这里发生一次，调用方只问 ``build_github_client()``。
"""

from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger("moa.code_review.github_provider")

FIXTURE_ENV = "CODE_REVIEW_GITHUB_FIXTURE"


def fixture_path() -> str:
    """离线通道的 fixture 路径；空串 = 走真 GitHub。"""
    import os

    return (os.getenv(FIXTURE_ENV) or "").strip()


def github_configured() -> bool:
    """GitHub 侧是否可用（不建连接，只判配置）。

    webhook 用它做 fail-fast：配置缺失时立刻如实降级，而不是入队一个注定失败的
    任务、让 worker 半夜报错。注意离线通道也算"已配置"——否则 demo 永远降级。
    """
    if fixture_path():
        return True
    from app.config import settings

    return bool(settings.github_token)


def build_github_client() -> Any:
    """真 GitHub 或离线通道。**离线时会在日志里明确说出来**。"""
    path = fixture_path()
    if path:
        from apps.code_review_pipeline.routing.fixture_github_client import (
            FixtureGitHubClient,
        )

        logger.warning(
            "%s is set: GitHub is replaced by a local fixture (%s). "
            "Review comments will NOT reach a real PR.",
            FIXTURE_ENV,
            path,
        )
        return FixtureGitHubClient(path)
    from apps.code_review_pipeline.routing.github_client import GitHubClient

    return GitHubClient.from_env()

