from __future__ import annotations
import logging, os
from opentelemetry import trace
from app.config import settings
from app.engine import Engine
from app.evaluator.evaluator import RuleEvaluator
from app.feature_flags import DEFAULT_FLAGS, FeatureFlagClient
from app.guard.guard_service import guard_service
# from app.guard.permission_guard import FailClosedPermissionGuard
from app.outbound.adapter import ResponseAdapter
from app.prompt_registry import PromptEntry, PromptRegistry
from app.router.intent_router import IntentRouter
from app.vectordb import VectorDBClient
from app.vectordb.retriever import ContextRetriever
from app.channels.feishu import FeishuChannelAdapter, FeishuConfig
from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider
from app.channels.feishu_cards import FeishuCardSender
from app.memory import ConversationMemory
from app.knowledge import KnowledgeBase

logger = logging.getLogger("moa.gateway")
tracer: trace.Tracer = trace.get_tracer("moa-gateway")

# Shared infrastructure
_feishu_config: FeishuConfig | None = None
_card_sender: FeishuCardSender | None = None
_flag_client = FeatureFlagClient()
_prompt_registry = PromptRegistry()
_retriever = ContextRetriever(VectorDBClient())

# Module-level singletons
router = IntentRouter()
memory = ConversationMemory()
knowledge_base = KnowledgeBase(_retriever._client)
adapter = ResponseAdapter()
evaluator = RuleEvaluator()
# permission_guard = FailClosedPermissionGuard()  # removed: unused legacy guard
engine = Engine(router=router, adapter=adapter)


def init_feishu() -> None:
    global _feishu_config, _card_sender
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if app_id and app_secret:
        _feishu_config = FeishuConfig(app_id=app_id, app_secret=app_secret)
        auth_provider = FeishuTokenProvider(FeishuAuthConfig(app_id=app_id, app_secret=app_secret))
        _card_sender = FeishuCardSender(auth_provider)
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
    _prompt_registry.set_active("coder", "stable")
    _prompt_registry.set_active("general", "stable")
    _flag_client.seed(DEFAULT_FLAGS)
    logger.info("prompt registry initialized with defaults")
_redis_store = None

async def _close_redis():
    global _redis_store
    if _redis_store is not None:
        await _redis_store.close()
