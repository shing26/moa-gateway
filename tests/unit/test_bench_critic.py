"""§5.0 回边的判据解析：Critic 的一句话必须能落成"批 / 改"与可执行的下一步。"""

from __future__ import annotations

from bench.agent.critic import (
    APPROVE,
    REVISE,
    build_review_message,
    format_revise_instruction,
    parse_verdict,
    render_trajectory,
)
from bench.agent.types import Step


def test_marker_approved_is_approval() -> None:
    verdict = parse_verdict("APPROVED")
    assert verdict.approved is True
    assert verdict.instructions == ""


def test_marker_revise_carries_its_instructions() -> None:
    verdict = parse_verdict(f"{REVISE}: 读 app/x.py 第 12 行，把 return 改成 fail-closed")
    assert verdict.approved is False
    assert "fail-closed" in verdict.instructions


def test_json_verdict_is_understood() -> None:
    verdict = parse_verdict(
        '{"verdict": "revise", "instructions": "重写 app/middleware/auth.py 的保护前缀"}'
    )
    assert verdict.approved is False
    assert "保护前缀" in verdict.instructions


def test_revise_can_name_the_subtasks_to_rerun() -> None:
    """L2 的打回要指出重跑哪几个子任务——``targets`` 是那条缝。"""
    verdict = parse_verdict(
        '{"verdict": "revise", "instructions": "第一个子任务改错了", "targets": [0, 2]}'
    )
    assert verdict.approved is False
    assert verdict.targets == (0, 2)


def test_approve_has_no_targets() -> None:
    assert parse_verdict('{"verdict": "approve", "instructions": ""}').targets == ()


def test_json_object_wrapped_in_prose_is_still_a_verdict() -> None:
    verdict = parse_verdict(
        '我先看了一下。\n```json\n{"verdict": "approve", "instructions": ""}\n```\n以上。'
    )
    assert verdict.approved is True


def test_unparseable_review_round_fails_closed_to_revise() -> None:
    """点评既不是 approve 也读不出 verdict 时，保守判"打回"，绝不替 agent 说通过。"""
    verdict = parse_verdict("嗯，这块我再看看。")
    assert verdict.approved is False
    assert verdict.instructions  # 原文留下，至少是一句有落点的指令


def test_render_trajectory_names_the_tool_and_its_observation() -> None:
    steps = (
        Step(
            index=0,
            action="call_tool",
            tool="run_tests",
            arguments={"selector": "-k auth"},
            observation="退出码 1（未通过）",
            outcome="ok",
        ),
        Step(index=0, action="call_tool", tool="write_file", observation="已写入", outcome="ok"),
    )
    rendered = render_trajectory(steps)
    assert "run_tests" in rendered
    assert "write_file" in rendered
    assert "退出码 1" in rendered


def test_build_review_message_states_the_goal_and_the_trace() -> None:
    steps = (Step(index=0, action="call_tool", tool="write_file", observation="已写入"),)
    message = build_review_message(goal="让验证命令通过。", steps=steps)
    assert "让验证命令通过。" in message
    assert "write_file" in message
    assert APPROVE in message
    assert REVISE in message


def test_revise_instruction_is_marked_so_the_next_context_carries_it() -> None:
    text = format_revise_instruction("app/x.py 第 12 行还是放行的")
    assert "app/x.py 第 12 行还是放行的" in text
    assert "上一轮" in text
