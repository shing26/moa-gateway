"""探针：飞书卡片发不出去时，先分清是"没权限"还是"机器人不在群里"（不发送任何消息）。

为什么要有这个探针：code 200671 只有一个数字，它同时对应好几种完全不同的原因
（机器人不在该会话、群已解散、receive_id 类型与实际不符……），而调用方只看
``send_card`` 的 bool，返回 False 就什么都不说了——于是排查只能靠猜。

本脚本走 ``GET /open-apis/im/v1/chats/{chat_id}``（只读）与 ``GET .../members``
，据此回答两个问题：这个会话对我方应用可见吗？机器人是否在成员列表里。

    uv run python scripts/probe_feishu_channel.py
"""

from __future__ import annotations

import sys

import httpx
from dotenv import load_dotenv

BASE = "https://open.feishu.cn/open-apis"


def main() -> int:
    load_dotenv()
    import os

    app_id = os.getenv("FEISHU_APP_ID") or ""
    app_secret = os.getenv("FEISHU_APP_SECRET") or ""
    chat_id = os.getenv("FEISHU_HOME_CHANNEL") or ""

    if not (app_id and app_secret and chat_id):
        print("FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_HOME_CHANNEL 有缺失", file=sys.stderr)
        return 2

    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        body = resp.json()
        if body.get("code") != 0:
            print(f"取 tenant_access_token 失败: {body.get('code')} {body.get('msg')}")
            return 1
        token = body["tenant_access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"token 获取成功，chat_id = {chat_id}")

        # 1) 这个会话对应用可见吗？
        r = client.get(f"{BASE}/im/v1/chats/{chat_id}", headers=headers)
        b = r.json()
        print(f"\n查会话: code={b.get('code')} msg={b.get('msg')}")
        if b.get("code") == 0:
            data = b.get("data", {}).get("chat", {})
            print(f"  name      = {data.get('name')}")
            print(f"  status    = {data.get('chat_status')}")
            print(f"  chat_mode = {data.get('chat_mode')}")

        # 2) 机器人在成员里吗？
        m = client.get(f"{BASE}/im/v1/chats/{chat_id}/members", headers=headers)
        mb = m.json()
        print(f"\n查成员: code={mb.get('code')} msg={mb.get('msg')}")
        if mb.get("code") == 0:
            items = mb.get("data", {}).get("items", [])
            names = [
                f"{i.get('name')}({i.get('member_id')})" for i in items[:10]
            ]
            print(f"  成员数 {len(items)}: {', '.join(names)}")
        else:
            print("  读不到成员列表，通常意味着应用根本没被加进这个会话。")

        print(
            "\n结论指引：\n"
            "  code 200671 + 查不到成员 -> 机器人不在该会话。"
            "把应用加入群，或改 FEISHU_HOME_CHANNEL 指向一个机器人确实在里面的会话。\n"
            "  能查到但仍发送失败 -> 多半是 receive_id_type 不匹配"
            "（send_card 固定用 chat_id，而该 id 若是 open_id/user_id 就会失败）。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

