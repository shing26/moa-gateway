"""任务级状态机（D3）。

**为什么不复用 ``app/fsm/state_machine.py`` 的 TRANSITIONS**：那张表回答的是
"收到事件 X 且处于状态 Y 时，下一个状态是什么"——那是**事件路由**问题。而任务
状态机要回答的是"处于状态 Y 时，**哪些角色被允许做哪些事**"——那是 D3 引入独立
worker 之后才出现的**归属与并发**问题。两者维度不同：FSM 的边上不关心是谁在推动，
任务表必须区分 worker / 人工审批 / 写回。

所以表达方式也不同：一张 ``状态 → 允许动作集合`` 的表，而不是 ``(状态, 事件)``。
好处是"非法"这件事变得可枚举：不在允许集合里的动作，不是"没有定义转移"，
而是"当前状态明令禁止"，错误信息能说出**谁**被拒了。

动作到目标状态是固定的（``claim`` 永远去 ``running``），所以由 ``ACTION_TARGET``
单点声明，不散落在各处 if 里。
"""

from __future__ import annotations

from enum import Enum


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    POSTING = "posting"
    DONE = "done"
    FAILED = "failed"
    # 人工拒绝（计划里 `queued→…→done + failed` 之外新增的第 7 个状态）。
    #
    # 为什么不能塞进 FAILED：FAILED 语义是"系统没能做完，可 requeue 重入"，
    # 而人工拒绝是"人明确说了不要"——自动重跑等于不听人话。所以它必须是**独立
    # 终态**：既不被 requeue，也不被 done 的幂等判断当成"已写回成功"。
    REJECTED = "rejected"


class TaskAction(str, Enum):
    CLAIM = "claim"
    REQUEST_APPROVAL = "request_approval"
    APPROVE = "approve"
    REJECT = "reject"
    COMPLETE = "complete"
    FAIL = "fail"
    REQUEUE = "requeue"


# 状态 → 允许的动作集合。
#
# 三个刻意的设计：
#
# 1. ``DONE`` 不允许任何动作。终态不可回退，这样"重复投递"在状态机这一层就被
#    挡住，而不是依赖每个调用点自己记得检查。
# 2. ``FAILED`` 只允许 ``REQUEUE`` 与 ``FAIL`` 自身——**不允许**直接 claim。
#    计划里"failed 允许重入"的实现方式是显式 requeue 回 queued，这样每次重入在
#    state_transitions 里都留一条痕迹。若允许 failed 直接 claim，那次重入在
#    迁移记录里就和第一次运行长得一样，事后无法区分"第一次"和"第三次"。
# 3. ``REJECT`` 是终态但与 ``DONE`` 分开：人工拒绝的 PR 不该被当成"已完成"，
#    幂等判断（posted_review_id IS NULL）在两种终态下的处置不同。
ALLOWED_ACTIONS: dict[TaskState, frozenset[TaskAction]] = {
    TaskState.QUEUED: frozenset({TaskAction.CLAIM, TaskAction.FAIL}),
    TaskState.RUNNING: frozenset(
        {
            TaskAction.REQUEST_APPROVAL,
            TaskAction.COMPLETE,
            TaskAction.FAIL,
        }
    ),
    # 挂起中允许 approve / reject / fail：审批人永不因为 worker 崩了就被锁死。
    TaskState.WAITING_APPROVAL: frozenset(
        {TaskAction.APPROVE, TaskAction.REJECT, TaskAction.FAIL}
    ),
    TaskState.POSTING: frozenset({TaskAction.COMPLETE, TaskAction.FAIL}),
    TaskState.DONE: frozenset(),
    TaskState.REJECTED: frozenset(),
    TaskState.FAILED: frozenset({TaskAction.REQUEUE}),
}


ACTION_TARGET: dict[TaskAction, TaskState] = {
    TaskAction.CLAIM: TaskState.RUNNING,
    TaskAction.REQUEST_APPROVAL: TaskState.WAITING_APPROVAL,
    # APPROVE 直接进 POSTING：审批通过和"开始写回"是同一个动作的两面，中间
    # 没有任何可观测状态。曾经单独留了个 start_posting，但它与 approve 目标
    # 相同、是纯死词汇（同"新增 task_state 列"那次的判断：没有消费者的取值
    # 不该进词汇表）。
    TaskAction.APPROVE: TaskState.POSTING,
    TaskAction.REJECT: TaskState.REJECTED,
    TaskAction.COMPLETE: TaskState.DONE,
    TaskAction.FAIL: TaskState.FAILED,
    TaskAction.REQUEUE: TaskState.QUEUED,
}


class InvalidTaskTransition(Exception):
    """当前状态不允许该动作。

    单独一个异常类型（而不是复用 ``MoaError`` 的 ``INVALID_STATE_TRANSITION``）：
    聊天 FSM 的非法转移与任务状态机的非法转移发生在不同层、处置也不同——前者在
    HTTP 响应里降级，后者必须让 worker 停止处理这条消息并 ack（否则会被
    XAUTOCLAIM 无限重复投递）。
    """

    def __init__(self, state: TaskState, action: TaskAction) -> None:
        allowed = ", ".join(sorted(a.value for a in ALLOWED_ACTIONS.get(state, frozenset())))
        self.state = state
        self.action = action
        super().__init__(
            f"task in state {state.value!r} does not allow action {action.value!r}"
            + (f"; allowed: {allowed}" if allowed else "; state is terminal")
        )


def is_terminal(state: TaskState) -> bool:
    """终态不再接受任何动作。"""
    return not ALLOWED_ACTIONS.get(state, frozenset())


def can(state: TaskState, action: TaskAction) -> bool:
    return action in ALLOWED_ACTIONS.get(state, frozenset())


def next_state(state: TaskState, action: TaskAction) -> TaskState:
    """返回 ``action`` 在 ``state`` 下的目标状态；不允许则抛异常。"""
    if not can(state, action):
        raise InvalidTaskTransition(state, action)
    return ACTION_TARGET[action]


def initial_transition(at: str) -> list[dict[str, str | None]]:
    """任务首次入队时写进 ``state_transitions`` 的那一条。"""
    return [{"from": None, "to": TaskState.QUEUED.value, "at": at}]
