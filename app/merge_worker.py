"""合并通道的独立 worker 进程（ADR-021）。

定期轮询所有非终态合并记录的 CI 状态，推进状态机。CI 绿时发审批卡片，
CI 红时发失败信息卡片。

**独立进程**（不是线程）：合并通道与审查任务的状态线不同，共用一个 worker
会让两者的失败域耦合。独立进程让合并通道可以独立重启、独立扩缩。

启动：``python -m app.merge_worker``
"""

from __future__ import annotations

import asyncio
import logging
import os

from app.config import settings
from app.deps import delivery_run_store, merge_store
from apps.code_review_pipeline.delivery.audit import WalDeliveryAudit
from apps.code_review_pipeline.merge_comment_approval import (
    build_comment_approval_poller,
)
from apps.code_review_pipeline.merge_poller import MergePoller
from apps.code_review_pipeline.notifications.merge_notifier import MergeNotifier
from apps.code_review_pipeline.routing.github_provider import build_github_client

logger = logging.getLogger("moa.merge_worker")


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
    ) -> None:
        self._store = store
        self._github = github
        self._notifier = notifier
        self._interval_s = interval_s
        self._comment_approval = comment_approval
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
                await self._notifier.send_approval_card(record)
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
