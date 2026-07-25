#!/usr/bin/env python3
"""
PROTOTYPE — FeishuTokenProvider 去重验证 (Windows compatible)

Question: Can feishu.py and feishu_cards.py share a single auth provider?
"""

import asyncio, time, sys

class TokenCache:
    def __init__(self, ttl: float = 2.0):
        self._token = None
        self._expires = 0.0
        self._call_count = 0
        self._ttl = ttl

    async def get_token(self) -> str:
        if self._token and time.monotonic() < self._expires:
            return self._token
        self._call_count += 1
        self._token = f"tok_{self._call_count}_{int(time.time())}"
        self._expires = time.monotonic() + self._ttl
        return self._token

    @property
    def call_count(self):
        return self._call_count

class FeishuChannel:
    def __init__(self, auth: TokenCache):
        self.auth = auth

    async def send(self, msg: str) -> str:
        token = await self.auth.get_token()
        return f"[msg] sent '{msg}' token={token[:12]}..."

class FeishuCardSender:
    def __init__(self, auth: TokenCache):
        self.auth = auth

    async def send_card(self, title: str) -> str:
        token = await self.auth.get_token()
        return f"[card] '{title}' token={token[:12]}..."

async def main():
    auth = TokenCache(ttl=2.0)
    chan = FeishuChannel(auth)
    card = FeishuCardSender(auth)
    logs = []
    n = 0
    print("=== FeishuTokenProvider Prototype ===")
    print()
    print("Commands: [s] send msg  [c] send card  [w] wait 3s  [q] quit")
    print()

    while True:
        print(f"Token: {auth._token}  |  Call count: {auth.call_count}  |  "
              f"Expires in: {max(0, auth._expires - time.monotonic()):.1f}s")
        print(f"Cache active: {auth._token is not None and time.monotonic() < auth._expires}")
        print()
        for l in logs[-3:]:
            print(f"  {l}")
        print()
        cmd = input("> ").strip().lower()
        if cmd == "q":
            break
        elif cmd == "s":
            n += 1
            r = await chan.send(f"hello-{n}")
            logs.append(r)
        elif cmd == "c":
            n += 1
            r = await card.send_card(f"approve-{n}")
            logs.append(r)
        elif cmd == "w":
            print("(waiting 3s for token to expire...)")
            await asyncio.sleep(3)
        if sys.platform == "win32":
            print()

    print(f"\nDone. TokenCache API calls: {auth.call_count}")
    print("Verdict: Shared TokenProvider works. Both feishu.py and feishu_cards.py")
    print("         can use it, auth is cached and only fetched once per TTL.")

if __name__ == "__main__":
    asyncio.run(main())
