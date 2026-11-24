"""合并通道的独立 worker 进程（ADR-021）。

定期轮询所有非终态合并记录的 CI 状态，推进状态机。CI 绿时发审批卡片，
CI 红时发失败信息卡片。

**独立进程**（不是线程）：合并通道与审查任务的状态线不同，共用一个 worker
会让两者的失败域耦合。独立进程让合并通道可以独立重启、独立扩缩。

启动：``python -m app.merge_worker``
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.deps import delivery_run_store, merge_store
from apps.code_review_pipeline.delivery.audit import WalDeliveryAudit
from apps.code_review_pipeline.merge_comment_approval import (
    build_comment_approval_poller,
)
from apps.code_review_pipeline.merge_preflight import MERGE_WORKER_HEARTBEAT_KEY
from apps.code_review_pipeline.merge_poller import MergePoller
from apps.code_review_pipeline.notifications.merge_notifier import MergeNotifier
from apps.code_review_pipeline.routing.github_client import GitHubRepo
from apps.code_review_pipeline.routing.github_provider import build_github_client

logger = logging.getLogger("moa.merge_worker")

Heartbeat = Callable[[], Awaitable[None]]


def build_redis_heartbeat(
    interval_s: float, *, redis_url: str | None = None
) -> Heartbeat:
    """默认心跳：每轮写一次 Redis 键，带 TTL，供 ``/healthz`` 与启动自检读。"""
    ttl_s = max(60, math.ceil(3 * float(interval_s)))

    async def heartbeat() -> None:
        from redis.asyncio import Redis

        client = Redis.from_url(
            redis_url or settings.redis_url,
            socket_timeout=5.0,
            decode_responses=True,
            protocol=2,
        )
        try:
            payload = {
                "pid": os.getpid(),
                "interval_s": float(interval_s),
                "at": datetime.now(UTC).isoformat(),
            }
            await client.set(MERGE_WORKER_HEARTBEAT_KEY, json.dumps(payload), ex=ttl_s)
        finally:
            await client.aclose()

    return heartbeat


class MergeWorker:
    """合并通道的 worker：定期轮询 CI 状态，发审批卡片。"""

    def __init__(
        self,
        store,
        github,
        notifier: MergeNotifier,
        *,
        run_store=None,
        audit=None,
        interval_s: float = 60.0,
        comment_approval=None,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self._store = store
        self._github = github
        self._notifier = notifier
        self._interval_s = interval_s
        self._comment_approval = comment_approval
        self._heartbeat = heartbeat or build_redis_heartbeat(interval_s)
        self._poller = MergePoller(
            store,
            github,
            run_store=run_store,
            audit=audit,
        )

    async def run_forever(self) -> None:
        """定期轮询，直到进程被终止。"""
        logger.info(
            "merge worker starting (interval=%.1fs, channel=%s)",
            self._interval_s,
            settings.code_review_merge_channel or "(未配置)",
        )
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - worker 主循环：单轮任意失败记录后继续下一轮，进程不能退出
                logger.exception("merge worker loop iteration failed")
            await asyncio.sleep(self._interval_s)

    async def _run_once(self) -> None:
        """轮询一次：读 CI 状态，推进状态机，发卡片。"""
        result = await self._poller.poll_once()
        for record in result.approvals:
            try:
                pr_details = await self._fetch_pr_details(record)
                delivery = await self._notifier.send_approval_card(
                    record, pr_details
                )
                message_id = str(getattr(delivery, "message_id", "") or "")
                if message_id:
                    self._persist_card_message_id(record, message_id)
            except Exception:  # noqa: BLE001 - 审批卡片发送：任意异常记录后继续处理下一条，单卡失败不阻塞批量
                logger.exception(
                    "failed to send approval card for %s", record.task_key
                )
        # CI 红 → 失败信息卡片（失败 job 名 + 链接）。ADR-021 验收 2 要求这条路径
        # 存在；此前 notifier 有这个能力但 worker 从未调用它，于是"CI 红推失败信息"
        # 只活在一个直调 notifier 的单测里，真实运行中不会发生。
        for record in result.ci_failures:
            try:
                await self._notifier.send_ci_failure_card(record)
            except Exception:  # noqa: BLE001 - CI 失败卡发送：任意异常记录后继续处理下一条
                logger.exception(
                    "failed to send CI failure card for %s", record.task_key
                )
        # GitHub 原生合并审批（ADR-040）。放在 CI 轮询之后：本轮刚进入
        # awaiting_approval 的记录也能在同一轮被已有评论批准。poll_once 自己
        # fail-closed，读失败不会打断 worker 循环。开关关闭时依赖为 None，
        # 连一次评论 API 都不会调用。
        if self._comment_approval is not None:
            await self._comment_approval.poll_once()
        # 心跳写在轮询末尾：它证明的是"这一轮真的跑完了"，而不是"进程还活着"。
        # 写失败只告警——Redis 抖动不该让合并通道停摆（下一轮会重试）。
        try:
            await self._heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 心跳是附加信号：写失败不得中断轮询
            logger.warning("merge worker heartbeat write failed", exc_info=True)

    async def _fetch_pr_details(
        self, record
    ) -> dict[str, Any] | None:
        """Read the PR facts used to enrich the approval card.

        Display-only: any failure or missing field returns ``None`` so the
        notifier falls back to the original three-line card. Approval delivery
        must not depend on this call succeeding.
        """
        try:
            owner, _, name = record.repo.partition("/")
            pr = await self._github.get_pr(
                GitHubRepo(owner=owner, name=name), record.pr_number
            )
        except Exception as exc:  # noqa: BLE001 - 富化失败只降级展示，不能漏发审批卡
            logger.warning(
                "get_pr failed for %s; approval card falls back to 3 lines: %s",
                record.task_key,
                exc,
            )
            return None

        if not isinstance(pr, dict):
            logger.warning(
                "get_pr returned no PR data for %s; card falls back to 3 lines",
                record.task_key,
            )
            return None

        base = pr.get("base") or {}
        user = pr.get("user") or {}
        details = {
            "title": pr.get("title"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "changed_files": pr.get("changed_files"),
            "base": base.get("ref") if isinstance(base, dict) else None,
            "html_url": pr.get("html_url"),
            "login": user.get("login") if isinstance(user, dict) else None,
        }
        if not self._details_complete(details):
            logger.warning(
                "get_pr returned incomplete fields for %s; card falls back to 3 lines",
                record.task_key,
            )
            return None
        return details

    @staticmethod
    def _details_complete(details: dict[str, Any]) -> bool:
        for key in ("title", "base", "html_url", "login"):
            value = details.get(key)
            if not isinstance(value, str) or not value.strip():
                return False
        for key in ("additions", "deletions", "changed_files"):
            value = details.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
        return True

    def _persist_card_message_id(self, record, message_id: str) -> None:
        try:
            self._store.set_card_message_id(record.identity, message_id)
        except Exception:  # noqa: BLE001 - 写回消息 id 只为终态改卡，失败只告警
            logger.warning(
                "failed to persist card_message_id for %s",
                record.task_key,
                exc_info=True,
            )


def build_worker() -> MergeWorker:
    """装配 merge worker。"""
    from app.channels.feishu_cards import FeishuCardSender
    from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider

    card_sender = None
    if settings.feishu_app_id and settings.feishu_app_secret:
        auth = FeishuTokenProvider(
            FeishuAuthConfig(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret,
            )
        )
        card_sender = FeishuCardSender(auth)

    notifier = MergeNotifier(
        card_sender=card_sender,
        default_target=settings.code_review_merge_channel or None,
    )

    github = build_github_client()
    audit = WalDeliveryAudit()

    return MergeWorker(
        store=merge_store,
        github=github,
        notifier=notifier,
        run_store=delivery_run_store,
        audit=audit,
        interval_s=float(settings.code_review_merge_poll_interval_s),
        comment_approval=build_comment_approval_poller(
            store=merge_store,
            github=github,
            settings_obj=settings,
            run_store=delivery_run_store,
            audit=audit,
        ),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # Windows 下 psycopg 拒绝 ProactorEventLoop（与 app/__main__.py 同一处理）。
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    worker = build_worker()
    logger.info("merge worker starting")
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        logger.info("merge worker interrupted; shutting down")


if __name__ == "__main__":
    main()
