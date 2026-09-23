"""配置来源一致性守卫（2026-09-23）。

背景：同一个配置概念在多处各自读 env，且**接受集合或优先级不同** → "配好了却没
生效"。已实测两例，都不是偶发，是同一缺陷类别：

1. ``MOA_HITL_ENABLED`` vs ``HITL_ENABLED``：两个变量名、两个默认值（true / false），
   而 ``MOA_HITL_ENABLED`` 全仓库只有那一行在用 → 只设其一的部署让两条引擎给出
   相反答案。（5d29545 修）
2. embedding 维度：``app/config.py`` 优先 ``VECTOR_DB_EMBEDDING_DIM``，而
   ``rag/embeddings.py`` 与 ``storage/review_store.py`` **反向**优先
   ``CODE_REVIEW_EMBEDDING_DIM``；API Key 更是只认窄名，只设通用名
   ``EMBEDDING_API_KEY`` 时那两条路径拿到空串 → 静默 401。（本轮修）

CI 抓到一个、修掉那一个，**缺陷类别还在**。所以这里不是"再修一个 bug"，而是
"让这类 bug 进不来"：配置概念只允许有一个读取点（``app/config.py``）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# 允许读取 env 的唯一模块
CONFIG_MODULE = "app/config.py"
# 诊断脚本按设计就是检查原始 env（它要回答"用户到底配了什么"），单独放行
ALLOWED_READERS = {CONFIG_MODULE, "scripts/doctor.py"}

SCAN_DIRS = ("app", "apps")

# 概念 → 该概念涉及的全部 env 名（任何一个在别处被读就是分叉风险）
SINGLE_SOURCE_CONCEPTS = {
    "embedding 维度": (
        "EMBEDDING_DIM", "VECTOR_DB_EMBEDDING_DIM", "CODE_REVIEW_EMBEDDING_DIM",
    ),
    "embedding 凭据与端点": (
        "EMBEDDING_API_KEY", "CODE_REVIEW_EMBEDDING_API_KEY",
        "EMBEDDING_BASE_URL", "CODE_REVIEW_EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL", "CODE_REVIEW_EMBEDDING_MODEL",
    ),
    "HITL 开关": ("HITL_ENABLED", "MOA_HITL_ENABLED"),
    "检索库 DSN": (
        "VECTOR_DB_DSN", "CODE_REVIEW_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL",
    ),
}

_ENV_READ = re.compile(r"""(?:os\.getenv|os\.environ\.get)\(\s*["']([A-Z0-9_]+)["']""")


def _env_reads(path: Path) -> list[tuple[int, str]]:
    reads: list[tuple[int, str]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for name in _ENV_READ.findall(line):
            reads.append((line_no, name))
    return reads


def _offenders_for(names: tuple[str, ...]) -> list[str]:
    offenders: list[str] = []
    for rel_dir in SCAN_DIRS:
        for path in (REPO_ROOT / rel_dir).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWED_READERS:
                continue
            for line_no, name in _env_reads(path):
                if name in names:
                    offenders.append(f"{rel}:{line_no} 读 {name}")
    return offenders


@pytest.mark.parametrize("concept", sorted(SINGLE_SOURCE_CONCEPTS))
def test_concept_has_a_single_env_reader(concept: str) -> None:
    """每个配置概念只允许 ``app/config.py`` 读 env。

    允许多处读同一组 env 就等于允许它们**接受集合不同**——那正是上面两例的成因，
    而且分叉时不报错，只是"配了没生效"。
    """
    offenders = _offenders_for(SINGLE_SOURCE_CONCEPTS[concept])

    assert not offenders, (
        f"{concept} 出现了第二个 env 读取点（应统一走 app.config.settings）: {offenders}"
    )


@pytest.mark.asyncio
async def test_code_review_embedding_client_uses_settings(monkeypatch) -> None:
    """回归：code review 的 client 必须用 settings 解析出的 key。

    此前它只读 ``CODE_REVIEW_EMBEDDING_API_KEY``，于是只设通用名
    ``EMBEDDING_API_KEY`` 的部署在这条路径上静默 401（app/vectordb 那条却正常），
    而 docstring 还承诺了 "or OPENAI_API_KEY"。
    """
    from app.config import settings
    from apps.code_review_pipeline.rag.embeddings import _build_embedding_client

    monkeypatch.setattr(settings, "embedding_api_key", "sk-ONLY-GENERAL")
    monkeypatch.setattr(settings, "embedding_base_url", "http://localhost:11434/v1")

    client = _build_embedding_client()
    try:
        assert client.headers.get("Authorization") == "Bearer sk-ONLY-GENERAL"
    finally:
        await client.aclose()


def test_embedding_dimension_readers_agree(monkeypatch) -> None:
    """两个读取点必须对同一份配置算出同一维度（此前优先级相反）。"""
    from app.config import settings
    from apps.code_review_pipeline.rag.embeddings import embedding_dimension
    from apps.code_review_pipeline.storage.review_store import _embedding_dim

    monkeypatch.setattr(settings, "vector_db_embedding_dim", 512)

    assert _embedding_dim() == 512
    assert embedding_dimension() == 512


def test_rogue_hitl_env_name_is_gone() -> None:
    """``MOA_HITL_ENABLED`` 这个孤立变量名不能再出现。

    它是 5d29545 修掉的那条的化石：同一个开关两个名字、两个默认值。
    """
    offenders: list[str] = []
    for rel_dir in SCAN_DIRS:
        for path in (REPO_ROOT / rel_dir).rglob("*.py"):
            for line_no, name in _env_reads(path):
                if name == "MOA_HITL_ENABLED":
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{line_no}"
                    )
    assert not offenders, f"MOA_HITL_ENABLED 应统一为 HITL_ENABLED: {offenders}"
