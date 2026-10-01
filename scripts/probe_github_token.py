"""探针：检查 GITHUB_TOKEN 的形态与可用性（不打印 token 本身）。

存在意义：401 与"token 写错了"是完全不同的两件事，但配置里只有一行等号，
看不出来。本脚本区分四种情况——空 / 形态不对（引号、空格、换行）/
形态对但 GitHub 拒收（401，通常是过期或复制不全）/ 可用。

    uv run python scripts/probe_github_token.py
    uv run python scripts/probe_github_token.py --repo shing26/moa-gateway
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx
from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe GITHUB_TOKEN shape and validity")
    parser.add_argument("--repo", default="", help="顺带探测这个仓库是否可读")
    args = parser.parse_args(argv)

    load_dotenv()
    raw = os.getenv("GITHUB_TOKEN") or ""
    token = raw.strip()

    if not token:
        print("GITHUB_TOKEN 为空（.env 里是 GITHUB_TOKEN=）", file=sys.stderr)
        return 2

    problems = []
    if len(raw) != len(token):
        problems.append(f"首尾有空白（读入 {len(raw)} 字符，去空白后 {len(token)}）")
    if token[:1] in ('"', "'") or token[-1:] in ('"', "'"):
        problems.append("首尾有引号——若这里仍检出，说明有转义残留")
    if not all(c.isalnum() or c == "_" for c in token):
        bad = sorted({c for c in token if not (c.isalnum() or c == "_")})
        problems.append(f"含 token 不该有的字符: {bad}")

    print(f"长度     : {len(token)}")
    print(f"前缀     : {token[:11]}{'...' if len(token) > 11 else ''}")
    print(f"形态检查 : {'通过' if not problems else '不通过'}")
    for p in problems:
        print(f"  - {p}")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "moa-token-probe",
    }

    with httpx.Client(timeout=20.0) as client:
        resp = client.get("https://api.github.com/user", headers=headers)
        print(f"/user    : HTTP {resp.status_code}")
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("message", ""))
            except Exception:  # noqa: BLE001 - 非 JSON 响应
                detail = resp.text[:120]
            print(f"  GitHub 拒绝: {detail}")
            print(
                "  形态对但被拒 -> 多半是 token 过期 / 复制不全 / 已撤销。"
                "重新生成后整段替换 .env 那一行，末尾不要留空格。"
            )
            return 1
        print(f"  登录身份: {resp.json().get('login')}")

        if args.repo:
            r = client.get(f"https://api.github.com/repos/{args.repo}", headers=headers)
            print(f"{args.repo} : HTTP {r.status_code}")
            if r.status_code == 200:
                j = r.json()
                perms = j.get("permissions") or {}
                print(
                    f"  private={j.get('private')} "
                    f"default_branch={j.get('default_branch')} "
                    f"push={perms.get('push')} pull={perms.get('pull')}"
                )
            else:
                print("  仓库不可读（不存在、无权限，或仓库名拼错）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
