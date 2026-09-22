from __future__ import annotations
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from redis.asyncio import Redis

from app.fsm.state_machine import (
    Event,
    InvalidStateTransitionException,
    State,
    StateContext,
    next_state,
)
from app.memory import _SHARED_BRIDGE
from app.models.events import MoAEvent
from app.outbound.adapter import ResponseAdapter, OutboundResponse

logger = logging.getLogger("moa.engine")


@dataclass
class HitlRequest:
    session_id: str
    trace_id: str
    agent_output: str
    intent: str
    agent_name: str
    channel: str
    target: str
    created_at: float = 0.0
    # 审批来源：guard 策略判定（"review"）／评估器判定（"eval_review"）／
    # 自动处理失败后的升级（"failure_escalation"）。默认值让 Redis 里既有的
    # 挂起记录仍能反序列化（缺字段走默认），回调侧审计据此区分来源。
    hitl_kind: str = "review"


class RedisHitlStorage:
    KEY_PREFIX = "moa:hitl"
    DEFAULT_TTL = 3600

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        ttl: int = DEFAULT_TTL,
        enable_fallback: bool = True,
        timeout: float = 1.0,
        client: Any | None = None,
        retry_after: float = 30.0,
    ) -> None:
        self.url = url
        self.ttl = ttl
        self._enable_fallback = enable_fallback
        self._timeout = timeout
        self._client = client
        self._injected = client is not None
        self._connected = False
        self._bridge = _SHARED_BRIDGE
        self._using_memory = False
        self._memory: dict[str, str] = {}
        self._written: set[str] = set()
        self._retry_after = retry_after
        self._last_attempt = 0.0

    @staticmethod
    def key(trace_id: str) -> str:
        return f"{RedisHitlStorage.KEY_PREFIX}:{trace_id}"

    def _resolve(self) -> Any:
        if not self._using_memory and self._connected:
            return self._client
        if self._using_memory and time.monotonic() - self._last_attempt < self._retry_after:
            return None
        self._last_attempt = time.monotonic()
        try:
            client = self._bridge.call(self._connect_coro())
        except Exception as exc:
            self._fallback(exc)
            return None
        if client is None:
            self._fallback()
            return None
        self._client = client
        self._connected = True
        self._using_memory = False
        return self._client

    async def _connect_coro(self) -> Any:
        client = self._client
        if client is None:
            client = Redis.from_url(
                self.url,
                socket_timeout=self._timeout,
                decode_responses=True,
                protocol=2,
            )
        try:
            await client.ping()
        except Exception:
            if self._client is None:
                await client.aclose()
            return None
        logger.info("redis hitl storage connected: %s", self.url)
        return client

    def _fallback(self, exc: Exception | None = None) -> None:
        if not self._enable_fallback:
            raise ConnectionError("redis unavailable and fallback disabled") from exc
        if self._client is not None and not self._using_memory and not self._injected:
            try:
                self._bridge.call(self._client.aclose())
            except Exception:
                pass  # nosec B110
            self._client = None
        self._connected = False
        self._using_memory = True
        self._last_attempt = time.monotonic()
        logger.critical("redis hitl storage fallback to in-memory")

    def get(self, key: str) -> str | None:
        client = self._resolve()
        if client is None:
            return self._memory.get(key)
        try:
            return self._bridge.call(client.get(key))
        except Exception as exc:
            self._fallback(exc)
            return self._memory.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        client = self._resolve()
        if client is None:
            self._memory[key] = value
            self._written.add(key)
            return
        try:
            self._bridge.call(client.set(key, value, ex=ttl if ttl is not None else self.ttl))
            self._written.add(key)
        except Exception as exc:
            self._fallback(exc)
            self._memory[key] = value
            self._written.add(key)

    def delete(self, key: str) -> None:
        client = self._resolve()
        if client is None:
            self._memory.pop(key, None)
            self._written.discard(key)
            return
        try:
            self._bridge.call(client.delete(key))
            self._written.discard(key)
        except Exception as exc:
            self._fallback(exc)
            self._memory.pop(key, None)
            self._written.discard(key)

    def clear(self) -> None:
        for key in list(self._written):
            self.delete(key)
        self._written.clear()
        self._memory.clear()


class SessionStore:
    """Manages HITL requests pending human approval."""

    def __init__(self, storage: Any = None) -> None:
        self._pending_hitl: dict[str, HitlRequest] = {}
        self._storage = None
        if storage is not None:
            try:
                self._storage = storage() if callable(storage) else storage
            except Exception as exc:
                logger.warning("hitl storage unavailable, using in-memory: %s", exc)
                self._storage = None

    def store_hitl(self, session_id: str, request: HitlRequest) -> None:
        hitl_id = request.trace_id or session_id
        if self._storage is None:
            self._pending_hitl[hitl_id] = request
        else:
            payload = asdict(request)
            if not payload.get("created_at"):
                payload.pop("created_at", None)
            self._storage.set(self._storage.key(hitl_id), json.dumps(payload))
        logger.info(
            "hitl stored hitl_id=%s session=%s intent=%s",
            hitl_id, session_id, request.intent,
        )

    def get_hitl(self, hitl_id: str) -> HitlRequest | None:
        if self._storage is None:
            return self._pending_hitl.get(hitl_id)
        raw = self._storage.get(self._storage.key(hitl_id))
        if raw is None:
            return None
        try:
            return HitlRequest(**json.loads(raw))
        except Exception as exc:
            logger.warning("hitl payload corrupt hitl_id=%s: %s", hitl_id, exc)
            return None

    def remove_hitl(self, hitl_id: str) -> None:
        if self._storage is None:
            self._pending_hitl.pop(hitl_id, None)
        else:
            self._storage.delete(self._storage.key(hitl_id))
        logger.info("hitl resolved hitl_id=%s", hitl_id)

    def clear_all(self) -> None:
        if self._storage is None:
            self._pending_hitl.clear()
        else:
            self._storage.clear()


@dataclass
class SessionState:
    session_id: str
    context: StateContext
    state_stack: list[str]

    def __post_init__(self) -> None:
        if self.state_stack is None:
            self.state_stack = []


class Engine:
    def __init__(
        self,
        router: Any = None,
        adapter: ResponseAdapter | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self.router = router
        self.adapter = adapter or ResponseAdapter()
        self.session_store = session_store or SessionStore()
        self._session_states: dict[str, StateContext] = {}

    async def handle_event(self, event: MoAEvent) -> SessionState:
        previous = self._session_states.get(event.session_id)
        metadata = dict(previous.metadata) if previous else dict(event.context)
        ctx = StateContext(
            state=previous.state if previous else State.INIT,
            session_id=event.session_id,
            trace_id=event.trace_id,
            # 必须显式携带：StateContext 每次都是新建的，不带上就等于每次事件
            # 都把重试计数清零（此前该字段从未被读出，所以这个缺口没暴露过）。
            retry_count=previous.retry_count if previous else 0,
            metadata=metadata,
        )
        current = ctx.state
        logger.info("incoming=%s trace=%s text=%r", event.event.value, event.trace_id, event.text)
        state_stack = list(metadata.get("state_stack", []))
        try:
            current = next_state(current, event.event)
        except Exception as exc:
            logger.exception("invalid transition: %s", exc)
            raise
        ctx.state = current
        if event.event == Event.TASK_FAILED:
            ctx.retry_count += 1
        elif event.event in (Event.MESSAGE_RECEIVED, Event.RESET, Event.CANCEL):
            # 重试预算是"每个请求"的，不是"每个会话"的：新请求或重置即归零。
            ctx.retry_count = 0
        if event.event in (Event.RESET, Event.CANCEL):
            state_stack = []
            ctx.metadata["hitl_pending"] = False
            ctx.metadata["sensitive_pending"] = False
        if event.event == Event.SENSITIVE_DETECTED:
            state_stack = list(state_stack) + [current.value]
            ctx.metadata["sensitive_pending"] = True
        elif event.event == Event.NEEDS_HUMAN:
            state_stack = list(state_stack) + [current.value]
            ctx.metadata["hitl_pending"] = True
        elif event.event == Event.HUMAN_APPROVED:
            ctx.metadata["hitl_pending"] = False
            if state_stack:
                state_stack = state_stack[:-1]
        elif event.event == Event.HUMAN_REJECTED:
            ctx.metadata["hitl_pending"] = False
            if state_stack:
                state_stack = state_stack[:-1]
        ctx.metadata["state_stack"] = state_stack
        self._session_states[event.session_id] = ctx
        return SessionState(session_id=event.session_id, context=ctx, state_stack=state_stack)

    def reset_session(self, session_id: str) -> None:
        self._session_states.pop(session_id, None)

    async def decide_hitl(
        self, *, session_id: str, trace_id: str, approve: bool
    ) -> tuple[StateContext | None, bool]:
        """把人工审批决定推进到 FSM，返回 ``(新状态, 是否已失效)``。

        为什么需要这个入口：``HitlRequest`` 落在 Redis（带 TTL），而 FSM 的会话状态
        只在**进程内**（``_session_states``）。服务重启后挂起记录还在、状态已经没了，
        此时 ``INIT + HUMAN_APPROVED`` 是非法迁移——此前表现为 webhook 路径 500、
        飞书路径"处理审批时出错了"，而用户再点一次仍然失败，卡片变成点一次错一次的砖。

        正确语义是"这套审批已失效"：调用方据此**消耗掉**挂起记录、明确告知用户重新
        发起，并写 ``guard_action="hitl_expired"`` 审计。两个回调入口（飞书 /
        webhook）共用本函数，避免两边语义漂移。
        """
        event = Event.HUMAN_APPROVED if approve else Event.HUMAN_REJECTED
        try:
            result = await self.handle_event(MoAEvent(
                trace_id=trace_id, event=event, session_id=session_id, text="",
                context={"source": "hitl_callback"},
            ))
        except InvalidStateTransitionException:
            logger.warning(
                "hitl decision cannot be applied (session state lost) session=%s trace=%s",
                session_id, trace_id,
            )
            return None, True
        return result.context, False

    def peek(self, session_id: str) -> StateContext | None:
        """Read-only view of a session's FSM context.

        Exists so callers that only need to *inspect* a session (the engine
        dispatcher deciding which engine handles a request) do not have to call
        ``handle_event``, which advances the state machine as a side effect.
        """
        return self._session_states.get(session_id)

    async def respond(self, session_state: SessionState, text: str, *, channel: str, target: str) -> OutboundResponse:
        return self.adapter.adapt(text, channel=channel, target=target)
