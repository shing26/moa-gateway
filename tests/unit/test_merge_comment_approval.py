"""GitHub 原生合并审批（ADR-040）的单测。

判定是纯函数，所以五条身份规则各用一条「只破坏一个条件」的对照实验来证明：

1. 身份只来自 API（正文里的名字无效）；
2. 白名单（大小写不敏感；空名单一条都不批）；
3. 作者不能自批；
4. 正文精确等于 ``APPROVE <bundle_hash>``；
5. 哈希必须绑定到记录上的 bundle_hash（为空则不批）。

轮询器一侧覆盖四种 fail-closed、幂等两轮、执行参数同构与「日志不含正文」。
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.config import Settings
from app.merge_worker import MergeWorker
from apps.code_review_pipeline.merge_comment_approval import (
    REASON_APPROVED,
    REASON_BODY_MISMATCH,
    REASON_EMPTY_BUNDLE_HASH,
    REASON_HASH_MISMATCH,
    REASON_IS_PR_AUTHOR,
    REASON_NOT_ALLOWLISTED,
    MergeCommentApprovalPoller,
    build_comment_approval_poller,
    comment_approval,
)
from apps.code_review_pipeline.merge_state import MergeAction, MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.routing.github_client import (
    CI_PENDING,
    GITHUB_API,
    CiStatus,
    GitHubClient,
    GitHubRepo,
    MergeOutcome,
)

BUNDLE_HASH = "abc123"
EXPECTED_BODY = f"APPROVE {BUNDLE_HASH}"


def _record(
    *,
    repo: str = "owner/repo",
    pr_number: int = 42,
    head_sha: str = "deadbeef",
    state: str = MergeState.AWAITING_APPROVAL.value,
    bundle_hash: str = BUNDLE_HASH,
    run_id: str = "run-1",
    trace_id: str = "trace-1",
) -> MergeRecord:
    return MergeRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        state=state,
        bundle_hash=bundle_hash,
        run_id=run_id,
        trace_id=trace_id,
    )


def _comment(login: str, body: str, *, comment_id: int = 1) -> dict:
    return {"id": comment_id, "user": {"login": login}, "body": body}


def _pr(login: str = "pr-author") -> dict:
    return {"user": {"login": login}}


# ── 纯函数：五条规则 ────────────────────────────────────────────────────


def test_allowlisted_non_author_is_approved() -> None:
    """正面用例：名单内、非作者、正文精确、哈希匹配 → 批。"""
    result = comment_approval(
        _record(),
        [_comment("alice", EXPECTED_BODY)],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is True
    assert result.reason == REASON_APPROVED
    assert result.login == "alice"


def test_login_case_does_not_matter() -> None:
    """正面用例：白名单 ``Alice``、API 作者 ``alice`` 仍通过（规则 2）。"""
    result = comment_approval(
        _record(),
        [_comment("alice", EXPECTED_BODY)],
        _pr("bob"),
        ("Alice",),
    )

    assert result.approved is True
    assert result.login == "alice"


def test_allowlist_entries_are_trimmed() -> None:
    """正面用例：白名单条目两侧空白不影响匹配。"""
    result = comment_approval(
        _record(),
        [_comment("alice", EXPECTED_BODY)],
        _pr("bob"),
        ("  alice  ",),
    )

    assert result.approved is True


def test_body_is_stripped_before_matching() -> None:
    """正面用例：正文首尾空白被 strip 掉（规则 4 的 ``strip()``）。"""
    result = comment_approval(
        _record(),
        [_comment("alice", f"  {EXPECTED_BODY}\n")],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is True


def test_outsider_is_rejected() -> None:
    """反例（规则 2）：只把作者换成名单外的人，其余条件全部成立。"""
    result = comment_approval(
        _record(),
        [_comment("mallory", EXPECTED_BODY)],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is False
    assert result.reason == REASON_NOT_ALLOWLISTED


def test_empty_allowlist_rejects_everything() -> None:
    """反例（规则 2）：白名单为空 = 通道未配置，即使正文完全正确也不批。"""
    result = comment_approval(
        _record(),
        [_comment("alice", EXPECTED_BODY)],
        _pr("bob"),
        (),
    )

    assert result.approved is False
    assert result.reason == REASON_NOT_ALLOWLISTED


def test_pr_author_cannot_self_approve() -> None:
    """反例（规则 3）：作者本人在名单里、正文正确，仍被拒。"""
    result = comment_approval(
        _record(),
        [_comment("alice", EXPECTED_BODY)],
        _pr("Alice"),
        ("alice",),
    )

    assert result.approved is False
    assert result.reason == REASON_IS_PR_AUTHOR


def test_missing_pr_author_fails_closed() -> None:
    """反例（规则 3 fail-closed）：拿不到 PR 作者 → 不批。"""
    result = comment_approval(
        _record(),
        [_comment("alice", EXPECTED_BODY)],
        {"user": {}},
        ("alice",),
    )

    assert result.approved is False
    assert result.reason == REASON_IS_PR_AUTHOR


def test_wrong_bundle_hash_is_rejected() -> None:
    """反例（规则 5）：只把哈希换掉，其余条件（名单、非作者、格式）全部成立。"""
    result = comment_approval(
        _record(),
        [_comment("alice", "APPROVE deadbeef")],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is False
    assert result.reason == REASON_HASH_MISMATCH


def test_body_with_extra_word_is_rejected() -> None:
    """反例（规则 4）：哈希正确，只在正文后面多加一个词。"""
    result = comment_approval(
        _record(),
        [_comment("alice", f"{EXPECTED_BODY} please")],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is False
    assert result.reason == REASON_BODY_MISMATCH


def test_body_without_hash_is_rejected() -> None:
    """反例（规则 4/5）：只有 ``APPROVE``、没有哈希，不算批准。"""
    result = comment_approval(
        _record(),
        [_comment("alice", "APPROVE")],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is False
    assert result.reason == REASON_BODY_MISMATCH


def test_body_is_case_sensitive() -> None:
    """反例（规则 4）：小写 ``approve`` 不算。"""
    result = comment_approval(
        _record(),
        [_comment("alice", "approve abc123")],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is False
    assert result.reason == REASON_BODY_MISMATCH


def test_identity_is_read_from_api_not_from_body() -> None:
    """反例（规则 1）：正文自称 alice，但 API 作者是名单外的 mallory。

    期望原因是 ``not_allowlisted`` 而不是正文/哈希原因——证明判定看的是
    ``user.login``，正文里的名字没有参与身份解析。
    """
    result = comment_approval(
        _record(),
        [_comment("mallory", f"{EXPECTED_BODY}\nI am alice")],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is False
    assert result.reason == REASON_NOT_ALLOWLISTED


def test_empty_bundle_hash_rejects_before_any_other_rule() -> None:
    """反例（规则 5 fail-closed）：记录没有 bundle_hash，任何正文都不批。"""
    result = comment_approval(
        _record(bundle_hash=""),
        [_comment("alice", "APPROVE ")],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is False
    assert result.reason == REASON_EMPTY_BUNDLE_HASH


def test_one_valid_comment_among_noise_is_enough() -> None:
    """多条评论中只要有一条满足全部规则就批准。"""
    result = comment_approval(
        _record(),
        [
            _comment("mallory", EXPECTED_BODY, comment_id=1),
            _comment("alice", "not yet", comment_id=2),
            _comment("alice", EXPECTED_BODY, comment_id=3),
        ],
        _pr("bob"),
        ("alice",),
    )

    assert result.approved is True
    assert result.login == "alice"


# ── 轮询器：执行参数、幂等、fail-closed ─────────────────────────────────


def _parse_task_key(task_key: str) -> tuple[str, int, str]:
    repo, _, rest = task_key.partition("#")
    pr_number, _, sha = rest.partition("@")
    return (repo, int(pr_number), sha)


class _FakeExecutor:
    """模拟真实 executor 的最小状态推进：APPROVE 后离开 AWAITING_APPROVAL。"""

    def __init__(self, store: MergeStore) -> None:
        self._store = store
        self.calls: list[tuple[str, str, str]] = []

    async def execute(self, task_key: str, action: str, operator: str) -> MergeOutcome:
        self.calls.append((task_key, action, operator))
        self._store.transition(
            _parse_task_key(task_key),
            MergeAction.APPROVE,
            at="2026-11-17T00:00:00+00:00",
            approver=operator,
        )
        return MergeOutcome(merged=False, status="approved")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _FakeGitHub:
    def __init__(
        self,
        *,
        comments: list[dict] | None = None,
        pr: dict | None = None,
        comments_error: Exception | None = None,
        pr_error: Exception | None = None,
    ) -> None:
        self.comments = comments if comments is not None else []
        self.pr = pr if pr is not None else _pr("bob")
        self.comments_error = comments_error
        self.pr_error = pr_error
        self.comment_calls: list[tuple[str, int]] = []
        self.pr_calls: list[tuple[str, int]] = []
        self.ci_calls: list[tuple[str, str]] = []

    async def list_issue_comments(self, repo, pr_number: int) -> list[dict]:
        self.comment_calls.append((repo.owner, pr_number))
        if self.comments_error is not None:
            raise self.comments_error
        return self.comments

    async def get_pr(self, repo, pr_number: int) -> dict:
        self.pr_calls.append((repo.owner, pr_number))
        if self.pr_error is not None:
            raise self.pr_error
        return self.pr

    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        self.ci_calls.append((repo.owner, ref))
        return CiStatus(state=CI_PENDING)


def _poller(
    store: MergeStore,
    github: _FakeGitHub,
    *,
    allowlist: tuple[str, ...] = ("alice",),
    executor: _FakeExecutor | None = None,
    recorder: _Recorder | None = None,
) -> tuple[MergeCommentApprovalPoller, _FakeExecutor, _Recorder]:
    fake_executor = executor if executor is not None else _FakeExecutor(store)
    fake_recorder = recorder if recorder is not None else _Recorder()
    poller = MergeCommentApprovalPoller(
        store,
        github,
        executor=fake_executor,  # type: ignore[arg-type]
        allowlist=allowlist,
        audit_recorder=fake_recorder,
    )
    return poller, fake_executor, fake_recorder


@pytest.mark.asyncio
async def test_execution_path_is_isomorphic_and_audited() -> None:
    """命中后：executor 参数为 github:<login>，审计带 bundle/run/trace 证据。"""
    store = MergeStore()
    record = _record()
    store.save(record)
    github = _FakeGitHub(comments=[_comment("alice", EXPECTED_BODY)])

    poller, executor, recorder = _poller(store, github)

    executed = await poller.poll_once()

    assert [r.task_key for r in executed] == [record.task_key]
    assert executor.calls == [(record.task_key, "approve", "github:alice")]
    assert len(recorder.calls) == 1
    audit = recorder.calls[0]
    assert audit["task_id"] == record.task_key
    assert audit["repo"] == record.repo
    assert audit["operator"] == "github:alice"
    assert audit["decision"] == "approve"
    assert audit["bundle_hash"] == BUNDLE_HASH
    assert audit["run_id"] == record.run_id
    assert audit["trace_id"] == record.trace_id
    # 审计里不得出现评论正文。
    assert EXPECTED_BODY not in repr(audit)


@pytest.mark.asyncio
async def test_two_rounds_execute_only_once() -> None:
    """幂等：第一轮执行后记录离开 AWAITING_APPROVAL，第二轮不再碰 API/执行器。"""
    store = MergeStore()
    record = _record()
    store.save(record)
    github = _FakeGitHub(comments=[_comment("alice", EXPECTED_BODY)])
    poller, executor, recorder = _poller(store, github)

    first = await poller.poll_once()
    second = await poller.poll_once()

    assert len(first) == 1
    assert second == ()
    assert len(executor.calls) == 1
    assert len(recorder.calls) == 1
    assert len(github.comment_calls) == 1
    assert store.get(record.identity).state == MergeState.MERGING.value


@pytest.mark.asyncio
async def test_comments_api_failure_is_fail_closed() -> None:
    """fail-closed 1/4：评论 API 抛异常 → 不执行、不抛出。"""
    store = MergeStore()
    record = _record()
    store.save(record)
    github = _FakeGitHub(comments_error=RuntimeError("rate limited"))
    poller, executor, recorder = _poller(store, github)

    executed = await poller.poll_once()

    assert executed == ()
    assert executor.calls == []
    assert recorder.calls == []
    assert store.get(record.identity).state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_pr_metadata_failure_is_fail_closed() -> None:
    """fail-closed 2/4：PR 元数据读不到 → 不执行、不抛出。"""
    store = MergeStore()
    record = _record()
    store.save(record)
    github = _FakeGitHub(
        comments=[_comment("alice", EXPECTED_BODY)],
        pr_error=RuntimeError("403 forbidden"),
    )
    poller, executor, _ = _poller(store, github)

    executed = await poller.poll_once()

    assert executed == ()
    assert executor.calls == []
    assert store.get(record.identity).state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_empty_allowlist_is_fail_closed_without_any_api_call() -> None:
    """fail-closed 3/4：白名单为空 → 连 GitHub 都不读，更不执行。"""
    store = MergeStore()
    record = _record()
    store.save(record)
    github = _FakeGitHub(comments=[_comment("alice", EXPECTED_BODY)])
    poller, executor, recorder = _poller(store, github, allowlist=())

    executed = await poller.poll_once()

    assert executed == ()
    assert executor.calls == []
    assert recorder.calls == []
    assert github.comment_calls == []
    assert github.pr_calls == []
    assert store.get(record.identity).state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_empty_bundle_hash_is_fail_closed_without_any_api_call() -> None:
    """fail-closed 4/4：记录没有 bundle_hash → 不读 API、不执行。"""
    store = MergeStore()
    record = _record(bundle_hash="")
    store.save(record)
    github = _FakeGitHub(comments=[_comment("alice", "APPROVE ")])
    poller, executor, recorder = _poller(store, github)

    executed = await poller.poll_once()

    assert executed == ()
    assert executor.calls == []
    assert recorder.calls == []
    assert github.comment_calls == []
    assert store.get(record.identity).state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_one_failing_record_does_not_starve_the_next() -> None:
    """fail-closed 不能中断整轮：第一条读失败，第二条仍被处理。"""
    store = MergeStore()
    first = _record(repo="a/repo", pr_number=1)
    second = _record(repo="b/repo", pr_number=2, head_sha="cafebabe")
    store.save(first)
    store.save(second)
    github = _FakeGitHub(comments=[_comment("alice", EXPECTED_BODY)])
    # 第一次评论读取失败，第二次成功。
    original = github.list_issue_comments
    calls = {"n": 0}

    async def flaky(repo, pr_number: int) -> list[dict]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return await original(repo, pr_number)

    github.list_issue_comments = flaky  # type: ignore[method-assign]
    poller, executor, _ = _poller(store, github)

    executed = await poller.poll_once()

    assert [r.task_key for r in executed] == [second.task_key]
    assert executor.calls == [(second.task_key, "approve", "github:alice")]
    assert store.get(first.identity).state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_list_issue_comments_uses_issue_endpoint_and_paginates() -> None:
    """PR 对话评论走 ``/issues/{n}/comments``，且必须翻页（与 review 同一坑）。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(200, json=[{"id": i} for i in range(100)])
        return httpx.Response(200, json=[{"id": 100}])

    client = GitHubClient(token="t")
    client._client = httpx.AsyncClient(  # noqa: SLF001 - 替换 transport，与既有 client 用例同一手法
        base_url=GITHUB_API,
        headers={"Authorization": "Bearer t"},
        transport=httpx.MockTransport(handler),
    )
    try:
        comments = await client.list_issue_comments(GitHubRepo("o", "r"), 42)
    finally:
        await client._client.aclose()

    assert len(comments) == 101
    assert paths
    assert all(path == "/repos/o/r/issues/42/comments" for path in paths)
    assert len(paths) == 2  # 第一页满 100 条，继续翻第二页


@pytest.mark.asyncio
async def test_logs_never_contain_comment_body(caplog) -> None:
    """日志只写原因码与身份，不写评论正文。"""
    store = MergeStore()
    record = _record()
    store.save(record)
    secret = "TOPSECRET-COMMENT-BODY"
    github = _FakeGitHub(comments=[_comment("mallory", f"{EXPECTED_BODY} {secret}")])
    poller, _, _ = _poller(store, github)

    with caplog.at_level(logging.INFO, logger="moa.merge_comment_approval"):
        await poller.poll_once()

    assert secret not in caplog.text


# ── 开关与配置 ──────────────────────────────────────────────────────────


class _FakeSettings:
    def __init__(self, *, enabled: bool, logins: tuple[str, ...]) -> None:
        self.github_comment_approval_enabled = enabled
        self.github_approver_logins = logins


def test_config_defaults_are_off_and_unconfigured(monkeypatch) -> None:
    """开关默认关、白名单默认空；不依赖开发机 .env。"""
    monkeypatch.delenv("GITHUB_COMMENT_APPROVAL_ENABLED", raising=False)
    monkeypatch.delenv("GITHUB_APPROVER_LOGINS", raising=False)

    settings = Settings()

    assert settings.github_comment_approval_enabled is False
    assert settings.github_approver_logins == ()


def test_config_parses_switch_and_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_COMMENT_APPROVAL_ENABLED", "1")
    monkeypatch.setenv("GITHUB_APPROVER_LOGINS", " Alice , bob ,,")

    settings = Settings()

    assert settings.github_comment_approval_enabled is True
    assert settings.github_approver_logins == ("Alice", "bob")


def test_switch_off_builds_no_poller() -> None:
    """关闭时装配结果为 None：worker 连调用都不会发生。"""
    store = MergeStore()
    github = _FakeGitHub()

    poller = build_comment_approval_poller(
        store=store,
        github=github,
        settings_obj=_FakeSettings(enabled=False, logins=("alice",)),
    )

    assert poller is None


def test_switch_on_builds_a_poller_with_the_allowlist() -> None:
    store = MergeStore()
    github = _FakeGitHub()

    poller = build_comment_approval_poller(
        store=store,
        github=github,
        settings_obj=_FakeSettings(enabled=True, logins=("alice",)),
    )

    assert isinstance(poller, MergeCommentApprovalPoller)


@pytest.mark.asyncio
async def test_worker_round_with_switch_off_never_calls_comment_api() -> None:
    """开关未开：worker 跑一轮，评论 API 调用次数为 0。"""
    store = MergeStore()
    store.save(_record())
    github = _FakeGitHub(comments=[_comment("alice", EXPECTED_BODY)])

    class _Notifier:
        async def send_approval_card(self, record) -> None:
            raise AssertionError("no approval card expected")

        async def send_ci_failure_card(self, record) -> None:
            raise AssertionError("no failure card expected")

    worker = MergeWorker(
        store=store,
        github=github,
        notifier=_Notifier(),  # type: ignore[arg-type]
        comment_approval=build_comment_approval_poller(
            store=store,
            github=github,
            settings_obj=_FakeSettings(enabled=False, logins=("alice",)),
        ),
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert github.comment_calls == []
    assert github.pr_calls == []
    assert store.get(_record().identity).state == MergeState.AWAITING_APPROVAL.value
