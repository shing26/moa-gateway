"""证伪脚本：web 渠道的 HITL 卡片为什么会被飞书拒（code 200671）。

复现方式刻意绕开 LLM——直接拿一个 ``channel="web"`` 的卡片去调 ``send_card``。
理由：要触发 HITL 得先让 guard 给出 REVIEW 判定，那依赖模型输出；而这里的缺陷是
**结构性的**，与判定无关，只需要证明"``send_card`` 不看 channel"。

    uv run python scripts/probe_feishu_target_mismatch.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider  # noqa: E402
from app.channels.feishu_cards import ApprovalCard, FeishuCardSender  # noqa: E402

BASE = "https://open.feishu.cn/open-apis"


async def main() -> int:
    load_dotenv()
    app_id = os.getenv("FEISHU_APP_ID") or ""
    app_secret = os.getenv("FEISHU_APP_SECRET") or ""
    chat_id = os.getenv("FEISHU_HOME_CHANNEL") or ""
    if not (app_id and app_secret):
        print("FEISHU_APP_ID / FEISHU_APP_SECRET 缺失", file=sys.stderr)
        return 2

    auth = FeishuTokenProvider(FeishuAuthConfig(app_id=app_id, app_secret=app_secret))
    token = await auth.get_token()

    # dashboard /chat 路由是这么调的：channel="web"，target=session_id
    for label, target, channel in (
        ("web 渠道（dashboard 实际走这条）", "web:web-probe-1", "web"),
        ("飞书渠道（真实会话）", chat_id, "feishu"),
    ):
        card = ApprovalCard(
            session_id="probe",
            trace_id="probe",
            agent_name="probe",
            intent="review",
            agent_output="probe",
            channel=channel,
            target=target,
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{BASE}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                json=card.to_message_payload(),
            )
        body = resp.json()
        print(f"{label}")
        print(f"  receive_id = {target!r} (receive_id_type=chat_id)")
        print(f"  -> code={body.get('code')} msg={body.get('msg')}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

