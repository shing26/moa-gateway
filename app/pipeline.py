from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from app.agents.contract import AgentEnvelope, get_agent
import app.agents.loader
from app.channels.feishu_cards import ApprovalCard
from app.command_mode import MODES, parse_command
from app.config import settings
from app.engine import HitlRequest
from app.guard.guard_service import GuardianAction
from app.middleware.request_logger import log_request
from app.models.events import MoAEvent
from app.prompt_registry.canary import CanaryConfig, select_canary_version


@dataclass(frozen=True)
class PipelineResult:
    trace_id: str
    state: str
    intent: str
    text: str
    status: str
    need_human_review: bool = False
    fallback: str = ""


class MoAPipeline:
    def __init__(
        self,
        engine: Any,
        router: Any,
        memory: Any,
        adapter: Any,
        evaluator: Any,
        retriever: Any,
        prompt_registry: Any,
        flag_client: Any,
        guard_service: Any,
        command_mode: Any,
        card_sender: Any = None,
    ) -> None:
        self.engine = engine
        self.router = router
        self.memory = memory
        self.adapter = adapter
        self.evaluator = evaluator
        self.retriever = retriever
        self.prompt_registry = prompt_registry
        self.flag_client = flag_client
        self.guard_service = guard_service
        self.command_mode = command_mode
        self.card_sender = card_sender

    def set_card_sender(self, sender: Any) -> None:
        self.card_sender = sender

    async def run(
        self,
        event: MoAEvent,
        *,
        channel: str,
        target: str,
        request: Any | None = None,
    ) -> PipelineResult:
        start = time.monotonic()

        session_state = await self.engine.handle_event(event)
        state = session_state.context.state.value

        text = event.text.strip()
        if text.startswith("/"):
            parsed = parse_command(text)
            if parsed:
                cmd_key, label = parsed
                if cmd_key in ("help", ""):
                    help_text = "可用指令:\n/coding - 编程模式\n/translate - 翻译模式\n/search - 搜索模式\n/analyze - 分析模式\n/default - 默认模式"
                    if request is not None:
                        await log_request(
                            request, 200, (time.monotonic() - start) * 1000,
                            event.session_id, "command", "help", "", event.text, help_text,
                        )
                    return PipelineResult(
                        trace_id=event.trace_id, state="ROUTED", intent="help",
                        text=help_text, status="command",
                    )
                cmd_info = MODES.get(cmd_key, {})
                self.command_mode.set(event.session_id, cmd_info.get("intent") or "")
                mode_label = cmd_info.get("label", cmd_key)
                reply = "已切换至 " + mode_label + " 模式"
                if request is not None:
                    await log_request(
                        request, 200, (time.monotonic() - start) * 1000,
                        event.session_id, "command", cmd_key, "", event.text, reply,
                    )
                return PipelineResult(
                    trace_id=event.trace_id, state="ROUTED", intent=cmd_key,
                    text=reply, status="command",
                )
            reply = "未知指令，发送 /help 查看可用指令"
            if request is not None:
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, "command", "help", "", event.text, reply,
                )
            return PipelineResult(
                trace_id=event.trace_id, state="ROUTED", intent="help",
                text=reply, status="command",
            )

        intent, fallback = await self.router.route(event.text)
        forced = self.command_mode.get(event.session_id)
        if forced:
            intent = forced
        agent = get_agent(intent) or get_agent("general")
        agent_name = intent if agent else "general"
        for name in ("coder", "general"):
            if get_agent(name) is agent:
                agent_name = name
                break

        retrieval = await self.retriever.retrieve(event.text, session_id=event.session_id)

        canary_enabled = await self.flag_client.get("canary.enabled", False)
        canary_pct = await self.flag_client.get("canary.traffic_pct", 10)
        canary_config = CanaryConfig(enabled=bool(canary_enabled), traffic_pct=int(canary_pct))
        selected_prompt, selected_version = select_canary_version(
            event.session_id, self.prompt_registry, agent_name, canary_config,
        )

        conversation_history = self.memory.get_history(event.session_id)
        envelope = AgentEnvelope(
            trace_id=event.trace_id,
            session_id=event.session_id,
            user_raw_input=event.text,
            global_summary=retrieval.context,
            history=tuple(conversation_history),
            agent_local_slot={
                "intent": intent,
                "resource": intent,
                "prompt_version": selected_version,
                "system_prompt": selected_prompt.system_prompt if selected_prompt else "",
            },
        )

        try:
            raw_output = await agent.execute(envelope)
        except Exception:
            if request is not None:
                await log_request(
                    request, 500, (time.monotonic() - start) * 1000,
                    event.session_id, agent_name, intent, "error", event.text,
                    "agent execution failed",
                )
            return PipelineResult(
                trace_id=event.trace_id, state=state, intent=intent,
                text="agent execution failed", status="error",
            )

        eval_result = await self.evaluator.score(raw_output, intent)

        payload = {"intent": intent, "resource": intent, "role": os.environ.get("MOA_DEFAULT_ROLE", "operator")}
        guard_intent = intent
        guard_hitl = settings.hitl_enabled
        if "EXECUTION_REQUIRES_APPROVAL" in raw_output:
            guard_intent = "execute_code"
            guard_hitl = True
        verdict = self.guard_service.evaluate(agent_name, guard_intent, payload, hitl_enabled=guard_hitl)

        if verdict.action == GuardianAction.REVIEW:
            hitl_request = HitlRequest(
                session_id=event.session_id, trace_id=event.trace_id, agent_output=raw_output,
                intent=intent, agent_name=agent_name, channel=channel, target=target,
            )
            self.engine.session_store.store_hitl(event.session_id, hitl_request)
            if self.card_sender:
                card = ApprovalCard(
                    session_id=event.session_id, trace_id=event.trace_id, agent_name=agent_name,
                    intent=intent, agent_output=raw_output, channel=channel, target=target,
                )
                await self.card_sender.send_card(card)
            if request is not None:
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, agent_name, intent, "review", event.text, raw_output,
                )
            return PipelineResult(
                trace_id=event.trace_id, state="SUSPENDED", intent=intent,
                text="Output requires human approval before delivery",
                status="pending_review", need_human_review=True,
            )

        if verdict.action == GuardianAction.DENY:
            if request is not None:
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, agent_name, intent, "deny", event.text, verdict.reason,
                )
            return PipelineResult(
                trace_id=event.trace_id, state=state, intent=intent,
                text=verdict.reason, status="blocked",
            )

        response = self.adapter.adapt(raw_output, channel=channel, target=target)
        self.memory.add(event.session_id, event.text, response.text)
        if request is not None:
            await log_request(
                request, 200, (time.monotonic() - start) * 1000,
                event.session_id, agent_name, intent, verdict.action.value, event.text, response.text,
            )
        return PipelineResult(
            trace_id=event.trace_id, state=state, intent=intent,
            text=response.text, status="ok",
            need_human_review=eval_result.need_human_review or verdict.action != GuardianAction.ALLOW,
            fallback=fallback,
        )
