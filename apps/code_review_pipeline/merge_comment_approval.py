"""GitHub 原生合并审批通道（ADR-040）。

**为什么存在**：合并审批此前只有两条路——飞书卡片回调（要求公网地址，当前无监听）
与终端/HTTP 回调里自由输入的 ``approver``（不校验身份）。PR 评论是现成、带认证
作者身份的外部动作，又不需要入站地址：主动轮询即可。于是白名单里的登录名在 PR
上评论一行 ``APPROVE <bundle_hash>``，就是一次可验证的第二人批准。

**判定与执行分离**（本模块的两半）：

* ``comment_approval`` 是**纯函数**：输入记录、评论列表、PR 元数据、白名单，
  输出「批 / 不批 + 稳定原因码」。不联网、不写库，可以被逐条规则单测。
* ``MergeCommentApprovalPoller`` 只负责取数据、调判定、命中后交给
  ``MergeExecutor`` 执行，并写 ``record_human_decision`` 审计。它与
  ``app/routes/webhook.py:_handle_merge_approval`` 的执行路径同构。

**身份模型五条**，缺一不可（任一不满足都不批）：

1. 评论作者只认 API 的 ``comment["user"]["login"]``，**绝不**从正文解析；
2. 作者在 ``GITHUB_APPROVER_LOGINS`` 白名单里（大小写不敏感）；白名单为空即
   通道未配置，一条都不批；
3. 作者不是 PR 作者（``pr["user"]["login"]``，大小写不敏感）；拿不到 PR 作者
   就按同一条规则 fail-closed；
4. 正文 ``strip()`` 后**恰好**等于 ``APPROVE <record.bundle_hash>``；
5. 哈希必须绑定到记录上的 ``bundle_hash``；记录上为空时一条都不批。

**fail-closed**：所有 GitHub 读取失败都只记日志、跳过该记录，绝不放行，也绝不
打断 worker 循环。日志与审计里不出现评论正文。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.code_review_pipeline.merge_executor import MergeExecutor
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord
from apps.code_review_pipeline.routing.github_client import GitHubRepo
from apps.code_review_pipeline.task_audit import record_human_decision

logger = logging.getLogger("moa.merge_comment_approval")

APPROVE_COMMAND = "APPROVE"

# 稳定原因码。调用方（日志、测试、后续指标）只依赖这些字符串，不依赖正文。
REASON_APPROVED = "approved"
REASON_NOT_ALLOWLISTED = "not_allowlisted"
REASON_IS_PR_AUTHOR = "is_pr_author"
REASON_HASH_MISMATCH = "hash_mismatch"
REASON_BODY_MISMATCH = "body_mismatch"
REASON_EMPTY_BUNDLE_HASH = "empty_bundle_hash"

# 多条评论都没有批准时，返回"最接近通过"的那条的原因，便于排查：
# 哈希/正文问题说明身份和名单都过了，比"名单外"更值得看。
_REASON_RANK = {
    REASON_NOT_ALLOWLISTED: 0,
    REASON_IS_PR_AUTHOR: 1,
    REASON_BODY_MISMATCH: 2,
    REASON_HASH_MISMATCH: 3,
}

DecisionRecorder = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class CommentApproval:
    """判定结果。``approved`` 为真时 ``login`` 是 API 给出的评论作者。"""

    approved: bool
    reason: str
    login: str = ""


def _normalize_logins(allowlist: Sequence[str]) -> frozenset[str]:
    return frozenset(
        str(item).strip().lower() for item in allowlist if str(item).strip()
    )


def _user_login(payload: Mapping[str, Any] | None) -> str:
    """从 GitHub 对象（评论或 PR）里取 ``user.login``，取不到就是空串。"""
    if not isinstance(payload, Mapping):
        return ""
    user = payload.get("user")
    if not isinstance(user, Mapping):
        return ""
    return str(user.get("login") or "").strip()


def _looks_like_approval_for_another_bundle(body: str) -> bool:
    """正文是不是「格式正确、但哈希不是记录上那个」的批准。

    只有恰好一个 token 跟在 ``APPROVE`` 后面才算；多一个词是正文格式问题
    （``body_mismatch``），不是哈希问题。
    """
    parts = body.split()
    return len(parts) == 2 and parts[0] == APPROVE_COMMAND


def comment_approval(
    record: MergeRecord,
    comments: Sequence[Mapping[str, Any]],
    pr: Mapping[str, Any] | None,
    allowlist: Sequence[str],
) -> CommentApproval:
    """判断有没有一条评论构成对该 bundle 的有效批准。**纯函数**。

    按五条身份规则顺序判定；返回的第一个满足全部规则的非作者评论即为批准。
    没有批准时返回原因码，方便日志与单测区分「哪条规则挡住的」。
    """
    # 规则 5 的前半：没有 bundle 哈希就没有可绑定的对象，绝不能拿任意哈希放行。
    if not (record.bundle_hash or "").strip():
        return CommentApproval(False, REASON_EMPTY_BUNDLE_HASH)

    allowed = _normalize_logins(allowlist)
    # 规则 2：白名单为空 = 通道未配置。
    if not allowed:
        return CommentApproval(False, REASON_NOT_ALLOWLISTED)

    # 规则 3 的前半：PR 作者读不到就 fail-closed。原因码复用 is_pr_author——
    # 它就是「无法证明评论者不是作者」，与作者本人评论同样处理。
    pr_author = _user_login(pr).lower()
    if not pr_author:
        return CommentApproval(False, REASON_IS_PR_AUTHOR)

    expected = f"{APPROVE_COMMAND} {record.bundle_hash}"
    best = CommentApproval(False, REASON_NOT_ALLOWLISTED)

    for comment in comments:
        # 规则 1：身份只来自 API。``body`` 在这里只用于匹配命令，绝不参与身份判断。
        login = _user_login(comment)
        if not login or login.lower() not in allowed:
            candidate = CommentApproval(False, REASON_NOT_ALLOWLISTED, login)
        elif login.lower() == pr_author:
            # 规则 3：PR 作者不能自批。
            candidate = CommentApproval(False, REASON_IS_PR_AUTHOR, login)
        else:
            body = str(comment.get("body") or "").strip()
            if body == expected:
                # 规则 4 + 5：正文精确等于 ``APPROVE <bundle_hash>``。
                return CommentApproval(True, REASON_APPROVED, login)
            if _looks_like_approval_for_another_bundle(body):
                candidate = CommentApproval(False, REASON_HASH_MISMATCH, login)
            else:
                candidate = CommentApproval(False, REASON_BODY_MISMATCH, login)

        if _REASON_RANK.get(candidate.reason, -1) > _REASON_RANK.get(
            best.reason, -1
        ):
            best = candidate

    return best


def _repo_from_name(repo: str) -> GitHubRepo:
    """``owner/repo`` → ``GitHubRepo``。"""
    owner, _, name = repo.partition("/")
    return GitHubRepo(owner=owner, name=name)


class MergeCommentApprovalPoller:
    """取评论 → 纯函数判定 → 命中后执行合并 + 写审计。

    **只处理** ``AWAITING_APPROVAL``；执行前重新读记录状态，不用本轮的快照：
    另一个 worker 或上一轮可能已经推进了它。记录一旦离开该状态，后续轮次自然
    不再命中，这就是幂等。
    """

    def __init__(
        self,
        store: Any,
        github: Any,
        *,
        executor: MergeExecutor,
        allowlist: Sequence[str] = (),
        audit_recorder: DecisionRecorder = record_human_decision,
    ) -> None:
        self._store = store
        self._github = github
        self._executor = executor
        self._allowlist = tuple(allowlist)
        self._record_decision = audit_recorder

    async def poll_once(self) -> tuple[MergeRecord, ...]:
        """轮询一轮。返回本轮实际执行了批准的记录（供测试与调用方观察）。"""
        if not _normalize_logins(self._allowlist):
            # 白名单为空 = 通道未配置。连 API 都不调，避免把"没配置"误读成"没批准"。
            logger.warning(
                "GITHUB_APPROVER_LOGINS is empty; GitHub comment approvals "
                "are not configured (fail-closed)"
            )
            return ()

        executed: list[MergeRecord] = []
        for snapshot in self._store.list_open():
            if snapshot.state != MergeState.AWAITING_APPROVAL.value:
                continue
            if not (snapshot.bundle_hash or "").strip():
                logger.info(
                    "skip GitHub comment approval for %s: empty bundle_hash",
                    snapshot.task_key,
                )
                continue

            repo = _repo_from_name(snapshot.repo)
            try:
                comments = await self._github.list_issue_comments(
                    repo, snapshot.pr_number
                )
                # 没有评论就不用再花一次 PR 读取；两件事都不改变"不批"。
                if not comments:
                    logger.info(
                        "no GitHub comments on %s; nothing to approve",
                        snapshot.task_key,
                    )
                    continue
                pr = await self._github.get_pr(repo, snapshot.pr_number)
            except Exception as exc:  # noqa: BLE001 - fail-closed：GitHub 任一读取失败都只意味着"不能批准"，记日志后处理下一条，绝不打断 worker
                logger.warning(
                    "GitHub comment approval read failed for %s: %s",
                    snapshot.task_key,
                    exc,
                )
                continue

            # 判定与执行前都重新读记录：本轮开头看到的状态可能已经被别人推进。
            # 用 current 而不是 snapshot 做判定，避免拿旧快照的 bundle_hash 放行。
            current = self._store.get(snapshot.identity)
            if current is None or current.state != MergeState.AWAITING_APPROVAL.value:
                logger.info(
                    "skip GitHub comment approval for %s: state changed to %s",
                    snapshot.task_key,
                    getattr(current, "state", "missing"),
                )
                continue

            decision = comment_approval(current, comments, pr, self._allowlist)
            if not decision.approved:
                logger.info(
                    "no GitHub comment approval for %s: %s",
                    current.task_key,
                    decision.reason,
                )
                continue

            operator = f"github:{decision.login}"
            try:
                # 与 webhook 的合并审批同构：executor 负责状态机与真实合并。
                await self._executor.execute(current.task_key, "approve", operator)
            except Exception:  # noqa: BLE001 - 执行失败：记录后继续下一条；若状态已离开待批，下一轮不会重试，避免重复合并
                logger.exception(
                    "GitHub comment approval execution failed for %s",
                    current.task_key,
                )
                continue

            try:
                # 审计只写身份与 bundle 证据，**不写评论正文**。评论作者来自
                # GitHub API 的 ``user.login``，是已验证身份（A2）。
                await self._record_decision(
                    task_id=current.task_key,
                    repo=current.repo,
                    operator=operator,
                    decision="approve",
                    run_id=current.run_id,
                    trace_id=current.trace_id,
                    bundle_hash=current.bundle_hash,
                    channel="github_comment",
                    identity_verified=True,
                )
            except Exception:  # noqa: BLE001 - 审计是旁路：审批已执行，审计写失败只记录，不改变合并结果
                logger.exception(
                    "failed to record GitHub comment approval for %s",
                    current.task_key,
                )

            executed.append(current)
            logger.info(
                "GitHub comment approval executed for %s by %s",
                current.task_key,
                operator,
            )

        return tuple(executed)


def build_comment_approval_poller(
    *,
    store: Any,
    github: Any,
    settings_obj: Any,
    run_store: Any = None,
    audit: Any = None,
) -> MergeCommentApprovalPoller | None:
    """按开关装配轮询器；关着时返回 ``None``，worker 连调用都不会发生。"""
    if not settings_obj.github_comment_approval_enabled:
        return None
    executor = MergeExecutor(store, github, run_store=run_store, audit=audit)
    return MergeCommentApprovalPoller(
        store,
        github,
        executor=executor,
        allowlist=settings_obj.github_approver_logins,
    )
