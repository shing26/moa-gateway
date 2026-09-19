"""EngineDispatcher: which engine handles which request, and why.

The four fallback cases are the whole safety argument for ``ENGINE=langgraph``:
without them the switch would silently drop slash-commands, reset/cancel,
sensitive-message suspension and pending-approval sessions.
"""

from __future__ import annotations

import pytest

import app.orchestration.dispatch as dispatch_module
from app.engine import Engine
from app.fsm.state_machine import Event as FsmEvent
from app.models.events import MoAEvent, new_trace_id
from app.orchestration.dispatch import EngineDispatcher
from app.pipeline import PipelineResult


class RecordingRunner:
    """Stands in for either engine; records every request it was handed."""

    def __init__(self, name: str, *, fails: bool = False) -> None:
        self.name = name
        self.calls: list[MoAEvent] = []
        self.fails = fails

    async def run(self, event, *, channel, target, request=None):
        self.calls.append(event)
        if self.fails:
            raise RuntimeError(f"{self.name} exploded")
        return PipelineResult(
            trace_id=event.trace_id,
            state="ROUTED",
            intent="coding",
            text=f"{self.name} reply",
            status="ok",
            agent_name=self.name,
            guard_action="allow",
        )

    def describe(self) -> dict[str, str]:
        return {"engine": self.name}


class StubPipeline:
    """Minimal MoAPipeline stand-in that exposes an Engine for peek()."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine


def make_event(
    text: str = "hello",
    session_id: str = "s1",
    event: FsmEvent = FsmEvent.MESSAGE_RECEIVED,
) -> MoAEvent:
    return MoAEvent(
        trace_id=new_trace_id(),
        event=event,
        session_id=session_id,
        text=text,
        context={"source": "test"},
    )


def make_dispatcher(graph=None, *, engine=None):
    return EngineDispatcher(StubPipeline(engine or Engine()), graph), graph


def test_without_graph_everything_stays_on_fsm() -> None:
    dispatcher, _ = make_dispatcher(graph=None)
    assert dispatcher.engine_name == "fsm"
    assert dispatcher.describe() == {"engine": "fsm"}
    assert dispatcher.needs_fsm(make_event()) is True


def test_plain_message_goes_to_graph() -> None:
    dispatcher, _ = make_dispatcher(RecordingRunner("langgraph"))
    assert dispatcher.describe() == {"engine": "langgraph"}
    assert dispatcher.needs_fsm(make_event("解释一下这段代码")) is False


@pytest.mark.parametrize("text", ["/help", "  /coding", "/review owner/repo#1"])
def test_slash_commands_fall_back(text: str) -> None:
    dispatcher, _ = make_dispatcher(RecordingRunner("langgraph"))
    assert dispatcher.needs_fsm(make_event(text)) is True


@pytest.mark.parametrize(
    "event", [FsmEvent.RESET, FsmEvent.CANCEL, FsmEvent.SENSITIVE_DETECTED]
)
def test_control_events_fall_back(event) -> None:
    dispatcher, _ = make_dispatcher(RecordingRunner("langgraph"))
    assert dispatcher.needs_fsm(make_event("x", event=event)) is True


@pytest.mark.asyncio
async def test_pending_session_falls_back_without_touching_state() -> None:
    engine = Engine()
    dispatcher, _ = make_dispatcher(RecordingRunner("langgraph"), engine=engine)

    # A sensitive message suspends the session through the FSM.
    await engine.handle_event(
        make_event("报错 debug", event=FsmEvent.SENSITIVE_DETECTED)
    )
    suspended = engine.peek("s1")
    assert suspended is not None

    assert dispatcher.needs_fsm(make_event("那现在呢")) is True

    # Deciding must be read-only: the machine is exactly where it was.
    assert engine.peek("s1") is suspended
    assert engine.peek("s1").state.value == "SUSPENDED"


@pytest.mark.asyncio
async def test_dispatch_routes_to_graph_and_returns_its_result() -> None:
    graph = RecordingRunner("langgraph")
    fsm = RecordingRunner("fsm")
    dispatcher = EngineDispatcher(fsm, graph)

    result = await dispatcher.run(make_event("你好"), channel="test", target="t1")

    assert result.text == "langgraph reply"
    assert len(graph.calls) == 1
    assert fsm.calls == []


@pytest.mark.asyncio
async def test_dispatch_routes_slash_command_to_fsm() -> None:
    graph = RecordingRunner("langgraph")
    fsm = RecordingRunner("fsm")
    dispatcher = EngineDispatcher(fsm, graph)

    result = await dispatcher.run(make_event("/help"), channel="test", target="t1")

    assert result.text == "fsm reply"
    assert fsm.calls and graph.calls == []


@pytest.mark.asyncio
async def test_graph_failure_falls_back_to_fsm_once() -> None:
    graph = RecordingRunner("langgraph", fails=True)
    fsm = RecordingRunner("fsm")
    dispatcher = EngineDispatcher(fsm, graph)

    result = await dispatcher.run(make_event("你好"), channel="test", target="t1")

    assert result.text == "fsm reply"
    assert len(graph.calls) == 1
    assert len(fsm.calls) == 1


@pytest.mark.asyncio
async def test_graph_path_writes_exactly_one_request_log(monkeypatch) -> None:
    logged: list[dict] = []

    async def fake_log(
        request,
        status_code,
        duration_ms,
        session_id="",
        agent_name="",
        intent="",
        guard_action="",
        input_text="",
        output_text="",
        **kwargs,
    ):
        logged.append(
            {
                "session_id": session_id,
                "status_code": status_code,
                "agent_name": agent_name,
                "guard_action": guard_action,
                **kwargs,
            }
        )

    monkeypatch.setattr(dispatch_module, "log_request", fake_log)

    dispatcher = EngineDispatcher(RecordingRunner("fsm"), RecordingRunner("langgraph"))
    await dispatcher.run(make_event("你好"), channel="test", target="t1", request=object())

    assert len(logged) == 1
    assert logged[0]["agent_name"] == "langgraph"
    assert logged[0]["guard_action"] == "allow"


@pytest.mark.asyncio
async def test_fsm_path_is_not_logged_twice(monkeypatch) -> None:
    logged: list[dict] = []

    async def fake_log(*args, **kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(dispatch_module, "log_request", fake_log)

    dispatcher = EngineDispatcher(RecordingRunner("fsm"), RecordingRunner("langgraph"))
    # Falls back to FSM: MoAPipeline logs its own path, so the dispatcher must
    # stay silent or every fallback request is counted twice.
    await dispatcher.run(make_event("/help"), channel="test", target="t1", request=object())

    assert logged == []
