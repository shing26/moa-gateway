"""任务状态存储契约：两个实现的签名与语义必须一致（D3）。

签名一致这件事本身值得断言：内存版曾按 trace_id、PG 版按 identity 三元组，
于是 worker 在本地跑得通、接真库就崩。反射比对签名能在真库缺席时抓到它。
"""

from __future__ import annotations

import inspect

import pytest

from apps.code_review_pipeline.storage.review_store import (
    PostgresReviewStore,
    ReviewRecord,
    ReviewStore,
    TaskNotFound,
)
from apps.code_review_pipeline.task_state import (
    InvalidTaskTransition,
    TaskAction,
    TaskState,
)

IDENTITY = ("org/app", 7, "sha1")

TASK_METHODS = ("get_task_state", "claim_task", "transition_task")


def _record(identity: tuple[str, int, str]) -> ReviewRecord:
    repo, pr, sha = identity
    return ReviewRecord(
        trace_id=f"cr_{repo}#{pr}@{sha}",
        repo=repo,
        pr_number=pr,
        head_sha=sha,
        author="u",
        findings_count=1,
        need_human_review=False,
        raw={"title": "t"},
    )


@pytest.mark.parametrize("method", TASK_METHODS)
def test_both_stores_expose_the_same_signature(method: str) -> None:
    """两个实现的同名方法必须逐参数一致。"""
    mem = inspect.signature(getattr(ReviewStore, method))
    pg = inspect.signature(getattr(PostgresReviewStore, method))
    assert list(mem.parameters) == list(pg.parameters), f"{method} 的参数列表分叉了"


def test_save_seeds_task_state_as_queued() -> None:
    store = ReviewStore()
    assert store.get_task_state(IDENTITY) is None
    store.save(_record(IDENTITY))
    assert store.get_task_state(IDENTITY) == TaskState.QUEUED.value


def test_claim_moves_queued_to_running() -> None:
    store = ReviewStore()
    store.save(_record(IDENTITY))
    assert store.claim_task(IDENTITY) is True
    assert store.get_task_state(IDENTITY) == TaskState.RUNNING.value


def test_second_claim_is_refused() -> None:
    """认领必须是一次性的：第二次返回 False 而不是又跑一遍 5 个 agent。"""
    store = ReviewStore()
    store.save(_record(IDENTITY))
    assert store.claim_task(IDENTITY) is True
    assert store.claim_task(IDENTITY) is False
    assert store.get_task_state(IDENTITY) == TaskState.RUNNING.value


def test_claim_of_unknown_task_is_refused_not_raising() -> None:
    """消息指向不存在的任务时返回 False——worker 会 ack 掉它而不是无限重投。"""
    assert ReviewStore().claim_task(("nope/app", 1, "x")) is False


def test_transition_rejects_illegal_action() -> None:
    store = ReviewStore()
    store.save(_record(IDENTITY))
    with pytest.raises(InvalidTaskTransition):
        store.transition_task(IDENTITY, TaskAction.APPROVE.value)


def test_transition_on_unknown_task_raises_task_not_found() -> None:
    """不存在与状态非法是两回事，处置不同，不能混成一个异常。"""
    with pytest.raises(TaskNotFound):
        ReviewStore().transition_task(("nope/app", 1, "x"), TaskAction.CLAIM.value)


def test_happy_path_through_store() -> None:
    store = ReviewStore()
    store.save(_record(IDENTITY))
    assert store.claim_task(IDENTITY)
    assert store.transition_task(IDENTITY, "request_approval") == "waiting_approval"
    assert store.transition_task(IDENTITY, "approve") == "posting"
    assert store.transition_task(IDENTITY, "complete") == "done"


def test_terminal_task_cannot_be_reclaimed() -> None:
    """done 之后重复投递到达：认领失败，worker 据此跳过。

    这是"至少一次投递 + 幂等消费"里"幂等"那一半的落点。
    """
    store = ReviewStore()
    store.save(_record(IDENTITY))
    store.claim_task(IDENTITY)
    store.transition_task(IDENTITY, "complete")
    assert store.claim_task(IDENTITY) is False
    assert store.get_task_state(IDENTITY) == TaskState.DONE.value


def test_failed_task_needs_explicit_requeue() -> None:
    store = ReviewStore()
    store.save(_record(IDENTITY))
    store.claim_task(IDENTITY)
    store.transition_task(IDENTITY, "fail")
    assert store.get_task_state(IDENTITY) == TaskState.FAILED.value
    assert store.claim_task(IDENTITY) is False, "failed 被直接认领了，重入不留痕"
    assert store.transition_task(IDENTITY, "requeue") == TaskState.QUEUED.value
    assert store.claim_task(IDENTITY) is True
