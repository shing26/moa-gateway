"""推送审批端口与工厂（T1：从 CLI 下沉到 delivery 领域层）。

``TerminalPushApprovalPort`` 原样迁自 ``app/cli.py``：行为、文案、
fail-closed 分支与 ``identity_verified`` 语义都不变。``delivery`` 由此不再
反向依赖命令行模块，retry / 未来的独立入口都可以只带领域层运行。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any


class TerminalPushApprovalPort:
    """终端推送审批闸门（A2）：approver ≠ token 主人，fail-closed。

    操作者身份 = ``GITHUB_TOKEN`` 的登录名，用 ``get_authenticated_login()``
    从 API 读——终端里自称的 approver 永远不可验证，能验证的只有 token 主人。
    白名单语义与 ``app/middleware/auth.py:approver_gate_error`` 同一套三态：
    配了 ``GITHUB_APPROVER_LOGINS`` 只放行白名单成员且 ≠ token 主人；空白名单
    只在显式 ``GATEWAY_ALLOW_INSECURE=1`` 时放行（并标记 gate=insecure*）；
    空白名单且无 insecure 一律拒绝——不能回到门全开。比较全部大小写不敏感。

    ``get_authenticated_login()`` 失败（网络/凭证）会**抛异常**：那是身份读取的
    infra 故障，``DeliveryFlow`` 会把它映射成 ``NEEDS_HUMAN``/``INFRA_ERROR``；
    吞成 ``None`` 会伪装成「人拒绝了」，放行更是绝对禁止。非 TTY / EOF 的既有
    拒绝路径原样保留（``None`` → ``REJECTED``）。拒绝时向终端打印稳定原因码
    （``self_approval`` / ``not_allowlisted``）；审批输入内容不进任何日志。
    """

    def __init__(self, github: Any) -> None:
        self._github = github

    async def request(self, *, run_id: str, bundle) -> Any:
        from app.config import settings
        from app.middleware.auth import insecure_mode_enabled
        from apps.code_review_pipeline.change.pipeline import ChangeApproval

        print(f"run_id   : {run_id}")
        print(f"branch   : {bundle.branch}")
        print(f"bundle   : {bundle.bundle_hash}")
        print(f"patch    : {bundle.patch_hash}")
        print(f"verify   : exit={bundle.verify_exit_code}")
        # `isatty()` alone is not enough. A child spawned with stdin bound to
        # DEVNULL/NUL reports `isatty() == True` on this machine's Windows
        # Python, so the guard below lets it through and `input()` then raises
        # EOFError - which the flow maps to `infra_error`/`needs_human`, not the
        # `rejected` a denied gate must produce. Both no-stdin and non-TTY deny
        # here; an EOF while prompting denies below. Nothing may raise.
        try:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        except (ValueError, OSError):
            # Probing stdin can fail on its own: a closed file raises
            # `ValueError: I/O operation on closed file`. A gate that cannot
            # confirm an interactive operator must deny - raising would be
            # mapped to `infra_error`. Only these two: Ctrl-C must still
            # propagate rather than be turned into a denial.
            interactive = False
        if not interactive:
            print("non-interactive stdin; push approval denied")
            return None
        try:
            operator = (await asyncio.to_thread(input, "approver: ")).strip()
            answer = (
                await asyncio.to_thread(
                    input,
                    "type APPROVE to push this exact bundle: ",
                )
            ).strip()
        except EOFError:
            print("stdin closed; push approval denied")
            return None
        if not operator or answer != "APPROVE":
            return None
        # token 主人是唯一可验证的身份。读取失败必须上抛（flow 映射
        # INFRA_ERROR），不得吞成 None——那会把 infra 故障记成「人拒绝了」。
        operator_login = str(await self._github.get_authenticated_login())
        allowlist = settings.github_approver_logins
        insecure = insecure_mode_enabled(os.getenv("GATEWAY_ALLOW_INSECURE"))
        approver = operator.lower()
        login = operator_login.lower()
        gate = ""
        if approver == login:
            # 自审批：白名单模式和裸拒绝模式下都拒绝；只有显式 insecure 的
            # 逃生口放行，且 gate 如实标记 insecure_self_approval。
            if allowlist or not insecure:
                print("push approval denied: self_approval")
                return None
            gate = "insecure_self_approval"
        elif allowlist:
            if approver not in {item.lower() for item in allowlist}:
                print("push approval denied: not_allowlisted")
                return None
            gate = "allowlist"
        elif insecure:
            gate = "insecure"
        else:
            # 空白名单且无 insecure：拒绝。这是 2026-09-28 修的 fail-closed
            # 姿势在终端通道上的对齐（见 approver_gate_error）。
            print("push approval denied: not_allowlisted")
            return None
        return ChangeApproval(
            approver=operator,
            trace_id=bundle.trace_id,
            bundle_hash=bundle.bundle_hash,
            run_id=run_id,
            channel="terminal",
            identity_verified=False,
            operator_login=operator_login,
            gate=gate,
        )


def build_push_approval_port(github: Any, *, settings_obj: Any = None) -> Any:
    """按配置选择推送审批通道（A1）：默认终端，显式开启才走飞书。"""
    if settings_obj is None:
        from app.config import settings

        settings_obj = settings

    if not settings_obj.feishu_push_approval_enabled:
        return TerminalPushApprovalPort(github=github)

    from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider
    from app.channels.feishu_cards import FeishuCardSender
    from app.engine import RedisHitlStorage, SessionStore
    from apps.code_review_pipeline.delivery.feishu_approval import (
        FeishuPushApprovalPort,
    )
    from apps.code_review_pipeline.delivery.run_store import DeliveryRunStore

    print(
        "push approval: feishu "
        f"(timeout={settings_obj.feishu_push_approval_timeout_s}s)",
        file=sys.stderr,
    )
    auth = FeishuTokenProvider(
        FeishuAuthConfig(
            app_id=settings_obj.feishu_app_id,
            app_secret=settings_obj.feishu_app_secret,
        )
    )
    return FeishuPushApprovalPort(
        card_sender=FeishuCardSender(auth),
        session_store=SessionStore(
            storage=RedisHitlStorage(
                url=settings_obj.redis_url,
                enable_fallback=settings_obj.redis_enable_fallback,
            )
        ),
        run_store=DeliveryRunStore(settings_obj.delivery_run_record_dir),
        github=github,
        settings_obj=settings_obj,
    )
