"""合并通道的状态机（ADR-0021）。

**为什么不复用 ``task_state.py``**：两张表回答的问题不同。审查任务的状态线是
``queued -> running -> waiting_approval -> posting``——它描述的是"审查跑到哪一步"；
合并通道是 ``watching -> awaiting_approval -> merging``——它描述的是"这个 PR 的
CI 和审批到了哪一步"。硬塞进一个枚举会让 ``TaskState`` 变成"什么都能装"，而
ADR-020 已经吃过"词汇表里出现没有消费者的取值"的亏。

**但三条设计照搬**，因为它们是这套底座真正的资产，不是审查专有的：

1. ``状态 -> 允许动作集合`` 的表，而不是 ``(状态, 事件)``。这样"非法"是可枚举的，
   错误信息能说出**谁**被拒了。
2. 动作到目标状态由 ``ACTION_TARGET`` 单点声明，不散落在各处 if 里。
3. 终态不可回退——"重复投递"在状态机这一层就被挡住。

一条与审查不同的新约束：**审批期间 CI 变红，待批必须作废**。见
``ALLOWED_ACTIONS`` 里 ``AWAITING_APPROVAL`` 上的 ``CI_RED``。
"""

from __future__ import annotations

from enum import Enum


class MergeState(str, Enum):
    # 等 CI 出结论。PR 一打开就进这里。
    WATCHING = "watching"
    # CI 红了，失败信息已推给人。**不是终态**：CI 重跑后可能变绿。
    CI_FAILED = "ci_failed"
    # CI 绿了，卡片已发出，等人点。
    AWAITING_APPROVAL = "awaiting_approval"
    # 已批准，正在合并（这个状态窗口里崩溃，要靠人去查，不自动重试）。
    MERGING = "merging"
    MERGED = "merged"
    # 人工拒绝。与 FAILED 分开：拒绝是"人明确说不要"，不该被任何重试当成可恢复。
    REJECTED = "rejected"
    # 合并失败（冲突 / 被保护分支拒绝 / 权限不足）。**终态**：原因已经如实告诉人，
    # 要不要重来由人决定，不由系统悄悄重试。
    FAILED = "failed"


class MergeAction(str, Enum):
    CI_GREEN = "ci_green"
    CI_RED = "ci_red"
    APPROVE = "approve"
    REJECT = "reject"
    COMPLETE = "complete"
    FAIL = "fail"


# 状态 -> 允许的动作集合。
ALLOWED_ACTIONS: dict[MergeState, frozenset[MergeAction]] = {
    # 等 CI 期间只可能有两种结论；FAIL 是"读 CI 本身失败了"（网络、权限）。
    MergeState.WATCHING: frozenset(
        {MergeAction.CI_GREEN, MergeAction.CI_RED, MergeAction.FAIL}
    ),
    # CI 红了之后仍然要能收到"绿了"——否则一次红就永久锁死这个 PR，
    # 而 CI 重跑（修一下再推）是最常见的路径。
    MergeState.CI_FAILED: frozenset(
        {MergeAction.CI_GREEN, MergeAction.CI_RED, MergeAction.FAIL}
    ),
    # **CI_RED 必须在允许集合里**：卡片发出之后、人点之前，CI 可能被重跑或
    # 因为新 commit 变红。此时那张卡片描述的是一个已经不成立的结论——它必须
    # 能作废。少了这一条，人就可能批准并合并一个 CI 正红的版本。
    MergeState.AWAITING_APPROVAL: frozenset(
        {
            MergeAction.APPROVE,
            MergeAction.REJECT,
            MergeAction.CI_RED,
            MergeAction.FAIL,
        }
    ),
    # 合并中不允许再批准/拒绝：动作已经交出去了，此刻的状态由 GitHub 决定。
    MergeState.MERGING: frozenset({MergeAction.COMPLETE, MergeAction.FAIL}),
    MergeState.MERGED: frozenset(),
    MergeState.REJECTED: frozenset(),
    MergeState.FAILED: frozenset(),
}


ACTION_TARGET: dict[MergeAction, MergeState] = {
    MergeAction.CI_GREEN: MergeState.AWAITING_APPROVAL,
    MergeAction.CI_RED: MergeState.CI_FAILED,
    MergeAction.APPROVE: MergeState.MERGING,
    MergeAction.REJECT: MergeState.REJECTED,
    MergeAction.COMPLETE: MergeState.MERGED,
    MergeAction.FAIL: MergeState.FAILED,
}


class InvalidMergeTransition(Exception):
    """当前状态不允许该动作。

    独立异常类型，理由与 ``InvalidTaskTransition`` 相同：聊天 FSM 的非法转移在
    HTTP 响应里降级，而这里的非法转移必须让调用方**停止推进**并如实记录——它通常
    意味着"同一个事件被投递了两次"，静默吞掉会让状态和现实脱节。
    """

    def __init__(self, state: MergeState, action: MergeAction) -> None:
        allowed = ", ".join(sorted(a.value for a in ALLOWED_ACTIONS.get(state, frozenset())))
        self.state = state
        self.action = action
        super().__init__(
            f"merge in state {state.value!r} does not allow action {action.value!r}"
            + (f"; allowed: {allowed}" if allowed else "; state is terminal")
        )


def is_terminal(state: MergeState) -> bool:
    return not ALLOWED_ACTIONS.get(state, frozenset())


def can(state: MergeState, action: MergeAction) -> bool:
    return action in ALLOWED_ACTIONS.get(state, frozenset())


def next_state(state: MergeState, action: MergeAction) -> MergeState:
    """返回 ``action`` 在 ``state`` 下的目标状态；不允许则抛异常。"""
    if not can(state, action):
        raise InvalidMergeTransition(state, action)
    return ACTION_TARGET[action]


def should_notify_approval(state: MergeState, action: MergeAction) -> bool:
    """这一步之后该不该发审批卡片。

    只有"**进入** ``AWAITING_APPROVAL``"才发。``CI_FAILED`` 上再收到一次
    ``CI_GREEN`` 也是进入，所以 CI 重跑变绿同样会发卡片——这正是我们要的。
    反过来，从 ``AWAITING_APPROVAL`` 收到 ``CI_RED``（卡片作废）不发任何卡片，
    由调用方发一条"已作废"的通知。
    """
    return can(state, action) and ACTION_TARGET[action] is MergeState.AWAITING_APPROVAL


def initial_transition(at: str) -> list[dict[str, str | None]]:
    """PR 首次进入通道时写进 ``merge_transitions`` 的那一条。"""
    return [{"from": None, "to": MergeState.WATCHING.value, "at": at}]
