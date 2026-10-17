"""L2 的任务分解与 handoff 词汇：结构化子任务、上下文、可读性保守的解析。"""

from __future__ import annotations

import pytest

from bench.agent.planner import (
    PlanParseError,
    build_plan_message,
    build_plan_review_message,
    parse_plan,
    render_plan,
)


def test_json_array_plan_keeps_subtasks_in_order_with_context() -> None:
    plan = parse_plan(
        '[{"instruction": "把 auth.py 的 return True 改成 fail-closed",'
        ' "context": "测试断言 /knowledge/* 返回 401"},'
        ' {"instruction": "跑 run_tests 验证", "context": "改动已完成，需要复验"}]'
    )
    assert [item.instruction for item in plan.subtasks] == [
        "把 auth.py 的 return True 改成 fail-closed",
        "跑 run_tests 验证",
    ]
    assert plan.subtasks[0].index == 0
    assert plan.subtasks[1].index == 1
    assert "断言" in plan.subtasks[0].context


def test_json_object_wrapping_subtasks_is_accepted() -> None:
    plan = parse_plan(
        '{"subtasks": ['
        '{"instruction": "第一步", "context": "背景一"},'
        '{"instruction": "第二步", "context": "背景二"}]}'
    )
    assert [item.instruction for item in plan.subtasks] == ["第一步", "第二步"]


def test_json_fence_and_prose_around_the_array_are_tolerated() -> None:
    plan = parse_plan(
        '先拆一下：\n```json\n'
        '[{"instruction": "改 auth", "context": "失败断言在 tests/test_auth.py"}]\n'
        "```\n以上。"
    )
    assert [item.instruction for item in plan.subtasks] == ["改 auth"]


def test_planner_was_given_a_numbering_to_use() -> None:
    plan = parse_plan(
        '[{"index": 5, "instruction": "A", "context": "背景 A"},'
        ' {"index": 9, "instruction": "B", "context": "背景 B"}]'
    )
    # 下标只是位置，不采信模型自填的编号——顺序执行里位置才是真的。
    assert [item.index for item in plan.subtasks] == [0, 1]


def test_dependencies_survive_parsing() -> None:
    """``depends_on`` 不在读取时丢掉——L3 要靠它做定向重跑。"""
    plan = parse_plan(
        '[{"instruction": "A", "context": "背景 A"},'
        ' {"instruction": "B", "context": "背景 B", "depends_on": [0, 0, "x", -1]}]'
    )
    assert plan.subtasks[0].depends_on == ()
    assert plan.subtasks[1].depends_on == (0,)


def test_non_json_output_is_rejected() -> None:
    with pytest.raises(PlanParseError):
        parse_plan("1. 读失败输出定位根因\n2. 改 app/x.py 的判定\n3. 跑 run_tests")


def test_subtask_without_context_is_rejected() -> None:
    """上下文是 worker 开工的唯一输入；缺了就拒绝整份计划。"""
    with pytest.raises(PlanParseError, match="context"):
        parse_plan('[{"instruction": "改 auth.py"}]')


def test_unreadable_plan_raises_instead_of_fabricating_one() -> None:
    with pytest.raises(PlanParseError):
        parse_plan("我觉得这个任务可以先看看，具体怎么拆我还没想好")


def test_empty_plan_is_rejected() -> None:
    with pytest.raises(PlanParseError):
        parse_plan("[]")


def test_plan_message_names_the_task_and_the_output_shape() -> None:
    message = build_plan_message(goal="让验证命令通过。", max_subtasks=4)
    assert "让验证命令通过。" in message
    assert "instruction" in message
    assert "context" in message
    assert "4" in message


def test_plan_review_message_carries_goal_plan_and_results() -> None:
    plan = parse_plan(
        '[{"instruction": "第一步", "context": "背景一"},'
        ' {"instruction": "第二步", "context": "背景二"}]'
    )
    message = build_plan_review_message(
        goal="让验证命令通过。",
        plan=plan,
        results=(("第一次的结果", "第一次的轨迹"), ("第二次的结果", "第二次的轨迹")),
    )
    assert "让验证命令通过。" in message
    assert "第一步" in message
    assert "第二次的结果" in message
    assert "APPROVED" in message


def test_render_plan_marks_unrun_subtasks() -> None:
    plan = parse_plan(
        '[{"instruction": "第一步", "context": "背景一"},'
        ' {"instruction": "第二步", "context": "背景二"}]'
    )
    rendered = render_plan(plan, results=(("结果一", "轨迹一"),))
    assert "第一步" in rendered
    assert "结果一" in rendered
    assert "第二步" in rendered
