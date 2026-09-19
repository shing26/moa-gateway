from __future__ import annotations
import logging, os
from typing import Any
from opentelemetry import trace
from app.agents.provider import LLMClient, LLMConfig
from app.config import settings
from app.engine import Engine, RedisHitlStorage, SessionStore
from app.evaluator.evaluator import RuleEvaluator
from app.feature_flags import DEFAULT_FLAGS, FeatureFlagClient
from app.guard.guard_service import guard_service
# from app.guard.permission_guard import FailClosedPermissionGuard
from app.outbound.adapter import ResponseAdapter
from app.prompt_registry import PromptEntry, PromptRegistry
from app.router.intent_router import IntentRouter
from app.router.llm_classifier import LLMIntentClassifier
from app.vectordb import build_vector_client
from app.vectordb.retriever import ContextRetriever
from app.audit.es_writer import EsWriter, build_es_writer
from app.channels.feishu import FeishuChannelAdapter, FeishuConfig
from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider
from app.channels.feishu_cards import FeishuCardSender
from app.memory import ConversationMemory, RedisConversationStorage
from app.long_term_memory import LongTermMemory
from app.command_mode import CommandMode, parse_command
from app.knowledge import KnowledgeBase
from app.obsidian_sync import ObsidianVaultSync
from app.pipeline import MoAPipeline

logger = logging.getLogger("moa.gateway")
tracer: trace.Tracer = trace.get_tracer("moa-gateway")

# Shared infrastructure
_feishu_config: FeishuConfig | None = None
_card_sender: FeishuCardSender | None = None
_flag_client = FeatureFlagClient()
_prompt_registry = PromptRegistry()
# 后端由 VECTOR_DB_DSN 决定：为空即内存存储（现状），配置后由 PostgreSQL 接管。
vector_client = build_vector_client()
_retriever = ContextRetriever(vector_client)
# 长期记忆与知识库共用同一个向量后端；无 DSN 时退化为进程内存存储。
long_term_memory = LongTermMemory(vector_client)

# Module-level singletons
es_writer: EsWriter | None = None
memory = ConversationMemory(
    storage=RedisConversationStorage(
        url=settings.redis_url,
        enable_fallback=settings.redis_enable_fallback,
    )
)
knowledge_base = KnowledgeBase(vector_client)
obsidian_sync = ObsidianVaultSync.from_env(knowledge_base=knowledge_base)
command_mode = CommandMode()
adapter = ResponseAdapter()
evaluator = RuleEvaluator()
# permission_guard = FailClosedPermissionGuard()  # removed: unused legacy guard


def _has_llm_credentials(prefix: str) -> bool:
    return any(
        os.environ.get(name)
        for name in (f"{prefix}_API_KEY", "OPENAI_API_KEY", "OMNIROUTE_API_KEY")
    )


def _build_classifier(prefix: str, *, fallback_main: bool = False) -> LLMIntentClassifier | None:
    if os.environ.get(f"{prefix}_MODEL") and _has_llm_credentials(prefix):
        try:
            config = LLMConfig.from_env(prefix)
            if not config.api_key:
                config.api_key = os.environ.get("OPENAI_API_KEY", "")
            return LLMIntentClassifier(LLMClient(config))
        except Exception:
            return None
    if fallback_main and os.environ.get("LLM_MODEL") and _has_llm_credentials("LLM"):
        return LLMIntentClassifier(LLMClient(LLMConfig.from_env("LLM")))
    return None


def build_intent_router() -> IntentRouter:
    return IntentRouter(
        router_llm=_build_classifier("ROUTER_LLM", fallback_main=True),
        micro_llm=_build_classifier("MICRO_LLM"),
        router_timeout_ms=settings.router_llm_timeout_ms,
        micro_timeout_ms=settings.micro_llm_timeout_ms,
    )


router = build_intent_router()

engine = Engine(
    router=router,
    adapter=adapter,
    session_store=SessionStore(
        storage=RedisHitlStorage(
            url=settings.redis_url,
            enable_fallback=settings.redis_enable_fallback,
        )
    ),
)

fsm_pipeline = MoAPipeline(
    engine=engine,
    router=router,
    memory=memory,
    adapter=adapter,
    evaluator=evaluator,
    retriever=_retriever,
    prompt_registry=_prompt_registry,
    flag_client=_flag_client,
    guard_service=guard_service,
    command_mode=command_mode,
    card_sender=None,
    long_term_memory=long_term_memory,
)


def _select_orchestrator(fsm: Any) -> Any:
    """Pick the request-path orchestrator from ``ENGINE``.

    ``langgraph`` is an optional extra, so this fails safe: any import problem
    logs and keeps the FSM pipeline, which is also what the Docker image gets
    (it syncs without extras).
    """
    if settings.engine != "langgraph":
        if settings.engine not in ("", "fsm"):
            logger.warning("未知 ENGINE=%r，按 fsm 处理", settings.engine)
        return fsm
    try:
        from app.orchestration.dispatch import EngineDispatcher
        from app.orchestration.graph import LangGraphOrchestrator
    except Exception as exc:  # noqa: BLE001 - missing optional extra must not stop boot
        logger.warning("ENGINE=langgraph 但 langgraph 不可用，回退 fsm: %s", exc)
        return fsm
    graph = LangGraphOrchestrator.from_deps()
    logger.info("编排引擎: langgraph（未建模路径回落 fsm）")
    return EngineDispatcher(fsm, graph)


# 重绑定为 dispatcher：路由只在启动时 import 一次，之后读到的是最终对象。
pipeline = _select_orchestrator(fsm_pipeline)


def init_feishu() -> None:
    global _feishu_config, _card_sender
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if app_id and app_secret:
        _feishu_config = FeishuConfig(app_id=app_id, app_secret=app_secret)
        auth_provider = FeishuTokenProvider(FeishuAuthConfig(app_id=app_id, app_secret=app_secret))
        _card_sender = FeishuCardSender(auth_provider)
        pipeline.set_card_sender(_card_sender)
        logger.info("feishu card sender initialized")
    else:
        logger.warning("FEISHU_APP_ID / FEISHU_APP_SECRET not set; HITL cards disabled")


def init_prompts() -> None:
    _prompt_registry.register(PromptEntry(
        agent_name="coder", version="stable",
        system_prompt="You are a professional coding assistant.",
        metadata={"author": "system"},
    ))
    _prompt_registry.register(PromptEntry(
        agent_name="general", version="stable",
        system_prompt="You are a general-purpose assistant.",
        metadata={"author": "system"},
    ))
    _prompt_registry.register(PromptEntry(
        agent_name="review", version="stable",
        system_prompt=(
            "You coordinate GitHub pull request reviews. Given owner/repo#PR, "
            "gather the PR diff and produce a concise review summary."
        ),
        metadata={"author": "system"},
    ))
    _prompt_registry.set_active("coder", "stable")
    _prompt_registry.set_active("general", "stable")
    _prompt_registry.set_active("review", "stable")
    _flag_client.seed(DEFAULT_FLAGS)
    logger.info("prompt registry initialized with defaults")


def init_audit() -> None:
    global es_writer
    es_writer = build_es_writer(settings)
    if es_writer is not None:
        logger.info("es audit writer enabled: %s", settings.es_hosts)
    else:
        logger.info("es audit writer disabled (ES_HOSTS not set)")


init_audit()
_redis_store = None

async def _close_redis():
    global _redis_store
    if _redis_store is not None:
        await _redis_store.close()
