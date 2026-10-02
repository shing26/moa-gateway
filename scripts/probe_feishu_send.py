"""探针：真发一条最小卡片，把飞书返回的 code/msg 原样打印出来（不打印凭据）。

存在的理由：``FeishuCardSender.send_card`` 失败时只 ``return False``，调用方拿到的
只有一个 bool，于是 200671 这种"只有一个数字"的错误码被丢在地上，没人知道它到底
在说什么。这里把服务端响应原样带出来。

    uv run python scripts/probe_feishu_send.py
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

BASE = "https://open.feishu.cn/open-apis"


def main() -> int:
    load_dotenv()
    app_id = os.getenv("FEISHU_APP_ID") or ""
    app_secret = os.getenv("FEISHU_APP_SECRET") or ""
    chat_id = os.getenv("FEISHU_HOME_CHANNEL") or ""
    if not (app_id and app_secret and chat_id):
        print("凭据或 FEISHU_HOME_CHANNEL 缺失", file=sys.stderr)
        return 2

    with httpx.Client(timeout=20.0) as client:
        tok = client.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        ).json()
        if tok.get("code") != 0:
            print(f"取 token 失败: {tok.get('code')} {tok.get('msg')}")
            return 1
        headers = {"Authorization": f"Bearer {tok['tenant_access_token']}"}

        # **直接用应用自己的卡片构造**，不要手写 JSON。
        #
        # 第一版探针手写了一份 `{"text": ...}` 当 interactive 卡片的 content，
        # 飞书回 230099/200621（parse card json err）——那是**探针自己写错了卡片**，
        # 和 200671 毫无关系。用 to_message_payload() 才能保证发出去的字节与
        # send_card 完全一致，测的才是真问题。
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.channels.feishu_cards import ApprovalCard

        card = ApprovalCard(
            session_id="probe",
            trace_id="probe",
            agent_name="probe",
            intent="probe",
            agent_output="[moa] 卡片连通性自检",
            channel="feishu",
            target=chat_id,
        )
        payload = card.to_message_payload()
        print(f"卡片类型 {payload['msg_type']}，receive_id={payload['receive_id']}")

        # 与 send_card 完全一致的调用方式：receive_id_type=chat_id
        r = client.post(
            f"{BASE}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers=headers,
            json=payload,
        )
        body = r.json()
        print(f"HTTP {r.status_code}  code={body.get('code')}  msg={body.get('msg')}")
        if body.get("code") != 0:
            print("\n这条错误码的含义取决于失败环节；把 code 原样贴给飞书文档/工单最快。")
            return 1
        print("发送成功（如果群里真看到了这条，说明凭据与会话都没问题）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
