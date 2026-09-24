from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("moa.config")


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true"}


def _parse_int(value: str | None, default: int, *, name: str = "") -> int:
    """Parse an int, falling back to ``default`` on garbage.

    Deliberately forgiving: a malformed env var should not stop the gateway
    from booting. The timeout settings above parse with bare ``int()`` and will
    raise on bad input; new settings use this instead. When ``name`` is given,
    the fallback is announced via a warning so silent misconfiguration at
    least leaves a trace in the log.
    """
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        if name:
            logger.warning("配置项 %s=%r 不是合法整数，回退默认值 %s", name, value, default)
        return default


def _parse_float(value: str | None, default: float, *, name: str = "") -> float:
    """Parse a float, falling back to ``default`` on garbage (see _parse_int)."""
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        if name:
            logger.warning("配置项 %s=%r 不是合法数字，回退默认值 %s", name, value, default)
        return default


def _parse_es_hosts(value: str | None) -> list[str]:
    if not value:
        return []
    hosts: list[str] = []
    for item in value.split(","):
        item = item.strip()
        if item:
            hosts.append(item)
    return hosts


def _parse_sentinel_hosts(value: str | None) -> list[tuple[str, int]]:
    if not value:
        return []
    hosts: list[tuple[str, int]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 1)
        if len(parts) != 2:
            logger.warning("REDIS_SENTINEL_HOSTS 条目 %r 缺少 host:port，已忽略", item)
            continue
        host, port = parts
        try:
            hosts.append((host.strip(), int(port)))
        except ValueError:
            logger.warning("REDIS_SENTINEL_HOSTS 条目 %r 端口不是整数，已忽略", item)
            continue
    return hosts


class Settings:
    def __init__(self) -> None:
        self.env: str = os.getenv("MOA_ENV", "dev")
        # 编排引擎选择：fsm（默认，自研状态机）| langgraph（可选第二引擎）。
        # langgraph 需要 optional extra，未安装时 deps 会告警并回退 fsm。
        self.engine: str = (os.getenv("ENGINE", "fsm") or "fsm").strip().lower()
        self.redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.redis_sentinel_hosts: list[tuple[str, int]] = _parse_sentinel_hosts(
            os.getenv("REDIS_SENTINEL_HOSTS")
        )
        self.redis_sentinel_master: str = os.getenv("REDIS_SENTINEL_MASTER", "mymaster")
        self.redis_enable_fallback: bool = _parse_bool(os.getenv("REDIS_ENABLE_FALLBACK"), True)
        self.router_llm_timeout_ms: int = int(os.getenv("ROUTER_LLM_TIMEOUT_MS", "2000"))
        self.micro_llm_timeout_ms: int = int(os.getenv("MICRO_LLM_TIMEOUT_MS", "1000"))

        # ── 任务 Agent（拆解 / ReAct）──────────────────────────────────────
        # AGENT_LLM：任务 Agent 的拆解后端。mock（默认，正则切分，离线可跑）或
        # litellm（真模型，复用 LLM_* 配置）。此前它由 app/agent_core/task_agent.py
        # **直读 env**、且 .env.template 完全没记录，于是"有真拆解"这件事没人知道
        # 怎么打开——收编进这里，并进 test_config_consistency 的单一来源守卫。
        self.agent_llm: str = (os.getenv("AGENT_LLM", "mock") or "mock").strip().lower()
        # AGENT_MAX_STEPS：工具轮次 / ReAct 步数的**唯一**上限。此前同一概念有两个
        # 值——app/agents/stubs.py 的 MAX_TOOL_ROUNDS=3 与 ReActLoop(max_steps=8)——
        # 而 README 的架构图写的是"工具循环 <=3 轮"。统一到这里，默认取更严的 3。
        self.agent_max_steps: int = _parse_int(
            os.getenv("AGENT_MAX_STEPS"), 3, name="AGENT_MAX_STEPS"
        )
        self.hitl_enabled: bool = _parse_bool(os.getenv("HITL_ENABLED"), False)
        # 审批人白名单（逗号分隔的 open_id）。**非空即强制**：回调里点击者不在此列则拒绝，
        # 且**不消耗**挂起记录（不该让他人把待审批点没了）。留空 = 不校验（默认，保持既有行为），
        # 但那时"聊天里任何人点一下都能批准"——所以 README 与 .env.template 都写明。
        self.hitl_approver_ids: tuple[str, ...] = tuple(
            item.strip()
            for item in os.getenv("HITL_APPROVER_IDS", "").split(",")
            if item.strip()
        )
        self.feishu_verification_token: str = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
        # ⚠️ 未实现且**已删除字段**：加密模式（X-Lark-Signature 的 HMAC / 时间戳防重放，
        # 以及加密体的 AES 解密）没有接线，且无法对着真实加密回调验证，所以决定不做
        # （2026-09-23）。此前留着这个字段会让人以为它生效——实际配了它事件会被静默忽略。
        # 见 README 已知边界；要支持需：AES 解密 + 签名校验 + 时间戳窗口。
        self.feishu_encrypt_key: str = ""
        self.es_hosts: list[str] = _parse_es_hosts(os.getenv("ES_HOSTS", ""))
        self.es_index_prefix: str = os.getenv("ES_INDEX_PREFIX", "moa-audit")

        # ── 检索存储 (P4 / P5) ─────────────────────────────────────────────
        # 空 DSN = 内存存储，即当前行为，零变化；配置 DSN 后由 PostgreSQL +
        # pgvector 接管，上层消费者无需改动。
        self.vector_db_dsn: str = (
            os.getenv("VECTOR_DB_DSN", "")
            or os.getenv("CODE_REVIEW_DATABASE_URL", "")
            # 这两条是 code-review 侧历史沿用的名字，一并收编：review_store 过去
            # 自己读它们而 config 不认，于是只设 DATABASE_URL 的部署在网关上"有库"、
            # 在 review_store 上静默回落非持久化（同类分叉，2026-09-23）。
            or os.getenv("DATABASE_URL", "")
            or os.getenv("POSTGRES_URL", "")
        )
        self.vector_db_table: str = os.getenv("VECTOR_DB_TABLE", "gateway_documents")
        # 维度必须与 db/gateway_schema.sql 中的 vector(N) 一致。
        self.vector_db_embedding_dim: int = _parse_int(
            os.getenv("VECTOR_DB_EMBEDDING_DIM")
            or os.getenv("CODE_REVIEW_EMBEDDING_DIM"),
            1536,
        )
        self.vector_db_pool_min_size: int = _parse_int(
            os.getenv("VECTOR_DB_POOL_MIN_SIZE"), 1, name="VECTOR_DB_POOL_MIN_SIZE"
        )
        self.vector_db_pool_max_size: int = _parse_int(
            os.getenv("VECTOR_DB_POOL_MAX_SIZE"), 4, name="VECTOR_DB_POOL_MAX_SIZE"
        )
        self.vector_db_keyword_scan_limit: int = _parse_int(
            os.getenv("VECTOR_DB_KEYWORD_SCAN_LIMIT"), 2000, name="VECTOR_DB_KEYWORD_SCAN_LIMIT"
        )
        # strict=False：PG 不可用时降级并在 /healthz 暴露，不阻断启动。
        # 拿到稳定实例后建议置 1，让配置错误在启动阶段就暴露。
        self.vector_db_strict: bool = _parse_bool(os.getenv("VECTOR_DB_STRICT"), False)
        self.vector_db_auto_migrate: bool = _parse_bool(os.getenv("VECTOR_DB_AUTO_MIGRATE"), True)

        # ── Embedding ─────────────────────────────────────────────────────
        # 未配置则整条语义检索关闭，走 BD-01 关键词回退。仅在配置了
        # VECTOR_DB_DSN 时才会真正发起调用，因此不会影响现有的内存存储路径。
        self.embedding_api_key: str = (
            os.getenv("EMBEDDING_API_KEY", "")
            or os.getenv("CODE_REVIEW_EMBEDDING_API_KEY", "")
            or os.getenv("OPENAI_API_KEY", "")
        )
        self.embedding_base_url: str = (
            os.getenv("EMBEDDING_BASE_URL", "")
            or os.getenv("CODE_REVIEW_EMBEDDING_BASE_URL", "")
            or os.getenv("OPENAI_BASE_URL", "")
            or "https://api.openai.com/v1"
        )
        self.embedding_model: str = (
            os.getenv("EMBEDDING_MODEL", "")
            or os.getenv("CODE_REVIEW_EMBEDDING_MODEL", "")
            or "text-embedding-3-small"
        )
        self.embedding_timeout_s: float = _parse_float(
            os.getenv("EMBEDDING_TIMEOUT_S"), 10.0, name="EMBEDDING_TIMEOUT_S"
        )

        # ── 鉴权与端口（原 main.py / __main__.py 的旁路 env 读取，收编统一校验）──
        self.webhook_auth_token: str = os.getenv("WEBHOOK_AUTH_TOKEN", "")
        self.dashboard_password: str = os.getenv("DASHBOARD_PASSWORD", "")
        # 保留原始字符串：真值集合（1/true/yes/on）由 auth.insecure_mode_enabled
        # 判定，与 _parse_bool 的 {1,true} 刻意不同，不要"统一"两者。
        self.gateway_allow_insecure: str = os.getenv("GATEWAY_ALLOW_INSECURE", "")
        # 裸 int()：端口写错属于启动失败级错误，语义与旧 __main__ 入口一致。
        self.gateway_port: int = int(os.getenv("GATEWAY_PORT") or os.getenv("APP_PORT") or "8081")

        # ── 2026-09-24 收编：此前这些概念散落多处各自读 env（有的 config 根本不认），
        # 是 ADR-012 那类"同一概念多个读取点"的未关完实例。现在唯一的读取点在这里，
        # tests/unit/test_config_consistency.py 的守卫自动开始覆盖它们。
        # RBAC 的兜底角色：payload 未带 role 时的默认（guard / pipeline / graph 三处共用）。
        # 空串=未配置（与其他配置同一约定）：conftest 用置空来中和开发机的 .env。
        self.default_role: str = (os.getenv("MOA_DEFAULT_ROLE", "operator") or "operator").strip()
        # 飞书应用凭据（HITL 卡片 / 事件适配器 / 通知器 / 面板展示共用）。
        self.feishu_app_id: str = os.getenv("FEISHU_APP_ID", "")
        self.feishu_app_secret: str = os.getenv("FEISHU_APP_SECRET", "")
        # OTel 导出端点（tracing 初始化 + 面板展示）。
        self.otel_exporter_otlp_endpoint: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        # GitHub 令牌（PR 审查链路的唯一凭据来源）。
        self.github_token: str = os.getenv("GITHUB_TOKEN", "")
        # 审计日志目录与保留期。此前 .env.template 文档写了 LOG_DIR / LOG_RETENTION_DAYS，
        # 但**没有任何代码读它们**（wal 硬编码 "logs"/90，audit_stats 也硬编码 "logs"）——
        # 两个死旋钮，这里复活它们。
        self.log_dir: str = os.getenv("LOG_DIR", "logs")
        self.log_retention_days: int = _parse_int(
            os.getenv("LOG_RETENTION_DAYS"), 90, name="LOG_RETENTION_DAYS"
        )

        # ── 预算拦截（M6）─────────────────────────────────────────────────
        # 0 = 只核算不拦截（默认，行为与未引入预算层完全一致）；
        # >0 = per-session 累计成本达到限额后拒后续请求。
        self.budget_session_limit_usd: float = _parse_float(
            os.getenv("BUDGET_SESSION_LIMIT_USD"), 0.0, name="BUDGET_SESSION_LIMIT_USD"
        )

        # ── 上下文预算（上下文工程：裁剪与压缩）───────────────────────────
        # token 估算口径见 app/context_budget.py（CJK 按字计）。0 = 禁用该项预算，
        # 保持既有行为；本机 1024 上下文的演示模型建议两个都设 384。
        self.context_history_budget: int = _parse_int(
            os.getenv("CONTEXT_HISTORY_BUDGET"), 1024, name="CONTEXT_HISTORY_BUDGET"
        )
        self.context_summary_budget: int = _parse_int(
            os.getenv("CONTEXT_SUMMARY_BUDGET"), 1024, name="CONTEXT_SUMMARY_BUDGET"
        )

        self.validate()

    def validate(self) -> None:
        """启动期 fail-fast 校验：只拦截"配置了但非法"的值。

        约定：空串/未配置一律放行并回落默认——tests/conftest.py 依赖
        "置空即屏蔽"的语义，本项目对缺失配置保持 forgiving。校验在
        Settings.__init__ 尾部执行，因此 import 期即暴露错误配置
        （deps 在 import 期就会用这些值构造单例，lifespan 里再校验太晚）。
        """
        def _raw(*names: str) -> str:
            for name in names:
                raw = (os.getenv(name) or "").strip()
                if raw:
                    return raw
            return ""

        dim_raw = _raw("VECTOR_DB_EMBEDDING_DIM", "CODE_REVIEW_EMBEDDING_DIM")
        if dim_raw:
            try:
                dim = int(dim_raw)
            except ValueError:
                dim = -1
            if dim <= 0:
                raise ValueError(
                    f"VECTOR_DB_EMBEDDING_DIM 必须是正整数（与 db schema 的 vector(N) 一致），得到 {dim_raw!r}"
                )
        for name, value in (
            ("ROUTER_LLM_TIMEOUT_MS", self.router_llm_timeout_ms),
            ("MICRO_LLM_TIMEOUT_MS", self.micro_llm_timeout_ms),
            ("AGENT_MAX_STEPS", self.agent_max_steps),
        ):
            if value <= 0:
                raise ValueError(f"{name} 必须为正整数，得到 {value}")
        if self.vector_db_pool_min_size > self.vector_db_pool_max_size:
            raise ValueError(
                "VECTOR_DB_POOL_MIN_SIZE 不能大于 VECTOR_DB_POOL_MAX_SIZE "
                f"（min={self.vector_db_pool_min_size}, max={self.vector_db_pool_max_size}）"
            )
        if not 1 <= self.gateway_port <= 65535:
            raise ValueError(f"GATEWAY_PORT 必须在 1-65535 之间，得到 {self.gateway_port}")
        if self.budget_session_limit_usd < 0:
            raise ValueError(
                f"BUDGET_SESSION_LIMIT_USD 不能为负（0 表示只核算不拦截），得到 {self.budget_session_limit_usd}"
            )
        for name, value in (
            ("CONTEXT_HISTORY_BUDGET", self.context_history_budget),
            ("CONTEXT_SUMMARY_BUDGET", self.context_summary_budget),
        ):
            if value < 0:
                raise ValueError(f"{name} 不能为负（0 表示禁用该预算），得到 {value}")
        if self.log_retention_days <= 0:
            raise ValueError(f"LOG_RETENTION_DAYS 必须为正整数，得到 {self.log_retention_days}")

    def to_redis_config(self) -> dict[str, Any]:
        return {
            "url": self.redis_url,
            "sentinel_hosts": self.redis_sentinel_hosts,
            "sentinel_master": self.redis_sentinel_master,
            "enable_fallback": self.redis_enable_fallback,
        }


settings = Settings()
