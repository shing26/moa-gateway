"""ReviewRecord 到 DB 行的字段投影必须与 UNIQUE 约束对齐（2026-10-01）。

真库实证：唯一一行遗留数据的 repo 为空、pr_number=0，而 trace_id 却是合法的
cr_shing26/moa-gateway:...。根因是 PostgresReviewStore.save() 从 record.raw 取
repo / pr_number / title / base_sha / html_url / diff_url，而唯一的生产者
app.worker.record_from_result 传的是 raw={}，那些 .get(key, default)
 于是全部落到默认值。

为什么必须现在修：D2 刚把幂等键定成 UNIQUE(repo, pr_number, head_sha)。建在
('', 0, sha) 上的唯一约束，只在"同一仓库恰好同一 commit"时才拦得住重复；换个
仓库的同一个 sha 就会误判成同一件事而合并。上一轮把唯一约束从 trace_id 换成
业务三元组是对的，但三元组本身当时是空的。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest


class _Cursor:
    def __init__(self, log: list[dict[str, Any]]) -> None:
        self._log = log

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._log.append(params or {})


class _Conn:
    def __init__(self) -> None:
        self.log: list[dict[str, Any]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.log)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


@pytest.fixture
def saved_params(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """跑一次真实的 save()，返回它交给 psycopg 的参数。"""
    from apps.code_review_pipeline.storage.review_store import (
        PostgresReviewStore,
        ReviewRecord,
    )

    conn = _Conn()

    class _FakePsycopg:
        @staticmethod
        def connect(dsn: str, **kwargs: Any) -> _Conn:
            return conn

    monkeypatch.setitem(sys.modules, "psycopg", _FakePsycopg)
    store = PostgresReviewStore(dsn="postgresql://u:p@h:5432/db", _psycopg=_FakePsycopg)
    store.save(
        ReviewRecord(
            trace_id="cr_o/r#7@deadbeef",
            repo="shing26/moa-gateway",
            pr_number=7,
            head_sha="deadbeefcafe",
            author="shing26",
            findings_count=3,
            need_human_review=True,
            # 生产者（app.worker.record_from_result）就是传空 raw
            raw={},
        )
    )
    assert len(conn.log) == 1
    return conn.log


def test_identity_columns_come_from_dataclass_not_raw(
    saved_params: list[dict[str, Any]],
) -> None:
    """repo / pr_number 是幂等键的组成部分，必须取自 dataclass 字段。"""
    params = saved_params[0]
    assert params["repo"] == "shing26/moa-gateway", (
        f"repo 落库成了 {params['repo']!r}：save() 读了 raw，而生产者传的是空 dict"
    )
    assert params["pr_number"] == 7, f"pr_number 落库成了 {params['pr_number']!r}"


def test_head_sha_prefers_dataclass_over_raw(
    saved_params: list[dict[str, Any]],
) -> None:
    """head_sha 同理；raw 里有值也不能盖掉 dataclass 的权威值。

    这是唯一当前"侥幸正确"的字段——save() 写的是
    raw.get("head_sha", record.head_sha)，raw 为空时恰好回退到了 dataclass。
    统一改成直接读 dataclass，顺带消掉"只有一列有回退"的不一致。
    """
    assert saved_params[0]["head_sha"] == "deadbeefcafe"


def test_required_not_null_columns_are_populated(
    saved_params: list[dict[str, Any]],
) -> None:
    """title / base_sha / html_url / diff_url 是 NOT NULL 列。

    它们同样是 NOT NULL，所以之前写空串不会报错——但库里会留下一堆空标题的
    任务行，而 D3 的 worker 按状态捞活时无从判断哪行是真任务。这类"写进去
    合法、读出来无意义"的空值比报错更难查。
    """
    params = saved_params[0]
    for col in ("title", "base_sha", "html_url", "diff_url"):
        assert isinstance(params[col], str), f"{col} 类型异常: {params[col]!r}"


def test_record_route_projection_fills_identity_fields() -> None:
    """record_from_result 必须把 PR 上下文的真实字段填进 ReviewRecord。

    ReviewRecord 里已有 repo / pr_number / head_sha / author 几个字段，
    record_from_result 也确实赋了值——问题只出在 save() 不看它们。这条断言
    "生产者侧填了"，与上面几条"消费者侧读对"配合，才算闭环。
    """
    from app.worker import record_from_result

    class _Pr:
        repo = "shing26/moa-gateway"
        pr_number = 7
        head_sha = "deadbeefcafe"
        base_sha = "abcdef0123"
        author = "shing26"
        title = "add task queue"
        html_url = "https://github.com/shing26/moa-gateway/pull/7"
        diff_url = "https://github.com/shing26/moa-gateway/pull/7.diff"
        # PRContext 上确实有这个字段，生产函数用它填 changed_files_count。
        # 假对象少给一个字段，生产函数就该在这里炸——那才是"缺字段"该有的样子。
        changed_files: list[Any] = []

    class _Section:
        findings: list[Any] = []

    class _Result:
        trace_id = "cr_o/r#7@deadbeef"
        pr = _Pr()
        triage = _Section()
        static_analysis = _Section()
        semantic_review = _Section()
        test_coverage = _Section()
        report = _Section()
        overall_need_human_review = False

    record = record_from_result(_Result())
    assert record.repo == "shing26/moa-gateway"
    assert record.pr_number == 7
    assert record.head_sha == "deadbeefcafe"
