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
    # 2026-09-24：任务 Agent 的后端与步数上限。此前两者都由
    # app/agent_core/task_agent.py 直读 os.environ，绕过配置层；而"工具轮次上限"
    # 这个概念当时有两个值（stubs 的 3 与 ReActLoop 的 8）。收编进 app/config.py 后，
    # 这个守卫自动开始覆盖它们——再有人从别处读 env 就会红。
    "任务 Agent 后端与步数": ("AGENT_LLM", "AGENT_MAX_STEPS"),
    # 2026-09-24 第二批收编（扫描发现同类未关完的实例）：以下概念此前散落多处
    # 各自读 env，其中 config.py 甚至不认 MOA_DEFAULT_ROLE / FEISHU_APP_ID/SECRET /
    # OTEL_EXPORTER_OTLP_ENDPOINT / GITHUB_TOKEN / LOG_DIR（后两个连同 LOG_RETENTION_DAYS
    # 是 .env.template 文档里写了却没人读的死旋钮）；LLM_* 那组更进了一步——
    # dashboard 展示面自带的默认值（""）与客户端工厂的默认（gpt-4o-mini /
    # api.openai.com）**不一致**，未配置时面板报"未设置"而客户端在用默认模型。
    "RBAC 兜底角色": ("MOA_DEFAULT_ROLE",),
    "飞书应用凭据": ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
    "OTel 导出端点": ("OTEL_EXPORTER_OTLP_ENDPOINT",),
    "GitHub 令牌": ("GITHUB_TOKEN",),
    "审计日志目录": ("LOG_DIR",),
    "审计日志保留期": ("LOG_RETENTION_DAYS",),
    "主链路 LLM 配置": ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"),
    "入口鉴权凭据": ("WEBHOOK_AUTH_TOKEN", "DASHBOARD_PASSWORD"),
}

# 个别概念按需放行的读者（config.py 对所有概念放行，见 ALLOWED_READERS）。
# 放行必须给理由——"看起来是第二处读取点"与"语义上必须是活读/工厂"是两回事：
CONCEPT_ALLOWED_READERS: dict[str, set[str]] = {
    # LLM_* 有一个**客户端工厂**（LLMConfig.from_env，按前缀动态读 env——字面扫描
    # 本就看不见它，这里显式记录）与一个**组合根**（deps 构建分类器时判断主模型
    # 是否已配置）。
    "主链路 LLM 配置": {
        "app/agents/provider.py",
        "app/deps.py",
        # 展示快照是 ops 面板的唯一读取点：ops/config 的 POST 是运行时可写设施
        # （直接写 env），展示必须读**活** env 才能反映运行时改动。
        "app/services/llm_status.py",
    },
    # 侧栏的鉴权态是**故意的请求期活读**：AuthMiddleware 在 import 期就固化了
    # 自己那份，而侧栏要反映运行时改动——test_dashboard_routes 钉住这个差异。
    # 这是"一处有意的活读"，不是分叉。
    "入口鉴权凭据": {"app/rendering/dashboard_html.py"},
}

_ENV_READ = re.compile(r"""(?:os\.getenv|os\.environ\.get)\(\s*["']([A-Z0-9_]+)["']""")


def _env_reads(path: Path) -> list[tuple[int, str]]:
    reads: list[tuple[int, str]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for name in _ENV_READ.findall(line):
            reads.append((line_no, name))
    return reads


def _offenders_for(names: tuple[str, ...], concept: str | None = None) -> list[str]:
    extra = CONCEPT_ALLOWED_READERS.get(concept, set()) if concept else set()
    offenders: list[str] = []
    for rel_dir in SCAN_DIRS:
        for path in (REPO_ROOT / rel_dir).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWED_READERS or rel in extra:
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
    offenders = _offenders_for(SINGLE_SOURCE_CONCEPTS[concept], concept)

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
