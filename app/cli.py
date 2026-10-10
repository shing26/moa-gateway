"""golden path 的操作入口（D5）。

    python -m app.cli demo review 42
    python -m app.cli demo status "owner/repo#42@sha"
    python -m app.cli demo approve "owner/repo#42@sha"

**为什么 ``demo review`` 走真实 HTTP + 真实签名，而不是直接调内部函数。**
直接调会跳过这次要验的全部东西：HMAC 验签、幂等判据、入队、202 语义。换句话说，
那样跑出来的"成功"只能证明函数没崩，证明不了底座是通的。而这条命令存在的意义
就是让人在 90 秒内亲眼看到 <1s 返回 202、第二次 200、崩溃后被认领。

``demo status`` / ``demo approve`` 则**直接**调内部函数：它们是人在终端前的操作，
不走 HTTP 就不会额外需要一个审批端点（那是另一件事，不在本轮范围）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any


def _identity_from_task_key(task_key: str) -> tuple[str, int, str]:
    """``owner/repo#42@sha`` -> ``(repo, 42, sha)``。

    task_key 保持可解析是刻意的：CLI、审计 --task、错误信息全都用它当唯一句柄，
    而如果它是一串不透明哈希，这三处就都得另外维护一张映射表。
    """
    repo, sep, rest = task_key.partition("#")
    if not sep or "@" not in rest:
        raise ValueError(f"task_key 格式应为 owner/repo#<pr>@<sha>，收到 {task_key!r}")
    number, _, sha = rest.partition("@")
    return repo, int(number), sha


def _fixture_keys(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return sorted(json.load(fh).get("pulls", {}))


def _fixture_pull(path: str, repo: str, pr_number: int) -> dict[str, Any] | None:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh).get("pulls", {}).get(f"{repo}#{pr_number}")


async def _head_sha_for_demo(repo: str, pr_number: int, explicit: str | None) -> str:
    """决定这次投递的 head sha。

    **离线通道下必须取自 fixture**，不能自己推导一个：worker 是按 payload 里的 sha
    去建任务行的，而它从 fixture 读到的 `pr.head.sha` 若是另一个值，结论就会落到
    另一行任务上——于是 `demo status` 显示 queued、库里却多了一行 done。这不是
    "数据不一致的小问题"，而是 demo 会展示出一个假的成功。

    所以规则是：显式 ``--sha`` > fixture 里的值 > **向 GitHub 现取**。

    真 GitHub 模式下曾经在这里"按 (repo, pr) 稳定推导"一个假 sha，那个值只在
    离线通道下成立（fixture 的 sha 是我们自己定的）。接了真 token 之后它会立刻
    分叉：webhook 按假 sha 建任务行，而 worker 从 GitHub 取回**真** sha 再把结论
    写到真 sha 那一行——于是库里多出一行，`demo status` 盯着的那一行永远停在
    queued。现取真实 sha 同时保住了幂等演示：同一个 PR 在没有新 commit 时 sha
    不变，所以 `demo review 42` 连跑两次第二次仍然撞幂等。
    """
    if explicit:
        return explicit
    from apps.code_review_pipeline.routing.github_provider import fixture_path

    path = fixture_path()
    if path:
        # 文件读挪出事件循环：ruff ASYNC230。CLI 是同步入口，这层只是为了让
        # 两条取 sha 的路径（本地文件 / GitHub API）共用一个 async 函数。
        entry = await asyncio.to_thread(_fixture_pull, path, repo, pr_number)
        if entry is None:
            raise ValueError(
                f"fixture {path} 里没有 {repo}#{pr_number}；"
                f"现有：{', '.join(sorted(_fixture_keys(path))) or '(空)'}"
            )
        return str(entry.get("pr", {}).get("head", {}).get("sha", ""))

    from apps.code_review_pipeline.routing.github_client import GitHubRepo
    from apps.code_review_pipeline.routing.github_provider import build_github_client

    owner, _, name = repo.partition("/")
    github = build_github_client()
    try:
        pr = await github.get_pr(GitHubRepo(owner=owner, name=name), pr_number)
        return str(pr.get("head", {}).get("sha", ""))
    finally:
        close = getattr(github, "aclose", None)
        if close is not None:
            await close()


def build_webhook_payload(repo: str, pr_number: int, head_sha: str, action: str) -> dict[str, Any]:
    """一份形态真实的 ``pull_request`` 事件。

    字段取自 GitHub 的实际 schema（含 labels / requested_reviewers 这类 webhook
    带、但 ``pulls/:n`` API 不一定同形的字段），因为入队时落库的就是这份 payload
    ——字段名写错的话，库里的 labels/reviewers 会静默变空，而这正是 D2 修过的那类
    "不报错但数据丢了"。
    """
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "id": 900_000 + pr_number,
            "number": pr_number,
            "title": f"[demo] PR #{pr_number} 可靠性底座验收",
            "state": "open",
            "user": {"login": "shing26"},
            "head": {"sha": head_sha},
            "base": {"sha": "0" * 40},
            "html_url": f"https://github.com/{repo}/pull/{pr_number}",
            "diff_url": f"https://github.com/{repo}/pull/{pr_number}.diff",
            "labels": [{"name": "demo"}],
            "requested_reviewers": [{"login": "moa-bot"}],
        },
        "repository": {"full_name": repo},
        "sender": {"login": "shing26"},
    }


async def _post_webhook(payload: dict[str, Any], url: str, secret: str) -> tuple[int, dict[str, Any]]:
    import httpx

    from apps.code_review_pipeline.routing.github_signature import sign_payload

    # 签名必须对**实际发出的字节**算。所以自己序列化再把 content 交给 httpx；
    # 用 json= 让 httpx 序列化的话，两次序列化规则可能不同（分隔符 / 转义）。
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Delivery": f"demo-{payload['pull_request']['head']['sha'][:12]}",
                "X-Hub-Signature-256": sign_payload(raw, secret),
            },
        )
    try:
        return resp.status_code, dict(resp.json())
    except ValueError:
        return resp.status_code, {"raw": resp.text[:400]}


def cmd_demo_review(args: argparse.Namespace) -> int:
    from app.config import settings

    secret = settings.github_webhook_secret
    if not secret:
        print(
            "GITHUB_WEBHOOK_SECRET 未设置，网关会 fail-closed 拒掉这次投递。\n"
            "  设置方法：在 .env 里加一行 GITHUB_WEBHOOK_SECRET=<任意串>，两侧用同一个值。",
            file=sys.stderr,
        )
        return 2

    repo = args.repo
    try:
        head_sha = asyncio.run(_head_sha_for_demo(repo, args.pr_number, args.sha))
    except Exception as exc:  # noqa: BLE001 - CLI 要把取 sha 失败说成人话
        # 现取 sha 会撞上 GitHub 的 401/404（token 失效、PR 不存在），而这些
        # 在这里必须变成一句可执行的提示，而不是一段 httpx 栈。
        print(f"取不到 {repo}#{args.pr_number} 的真实 head sha：{exc}", file=sys.stderr)
        print(
            "  先用 `uv run python scripts/doctor.py` 看 github 那一项；"
            "离线演示请设 CODE_REVIEW_GITHUB_FIXTURE。",
            file=sys.stderr,
        )
        return 2
    payload = build_webhook_payload(repo, args.pr_number, head_sha, args.event)
    url = f"http://{args.host}:{args.port}/webhook/github/review"

    # 计时**之前**先把依赖 import 掉。
    #
    # ``_post_webhook`` 内部才 import httpx，而它整个包在计时区间内。`import httpx`
    # 在这台机器上实测 ~640ms，于是"投递耗时"量的其实是 Python 的 import：同一个
    # 网关用直连客户端量是 19ms，走 CLI 却是 625~763ms，差额几乎全在这一条 import
    # 上。这正是"把阈值从 1000 挪到 1500 来通过"最该避免的假绿。
    import httpx  # noqa: F401 - 副作用是把它装进 sys.modules

    from apps.code_review_pipeline.routing.github_signature import (  # noqa: F401
        sign_payload,
    )

    started = time.perf_counter()
    status, body = asyncio.run(_post_webhook(payload, url, secret))
    elapsed_ms = (time.perf_counter() - started) * 1000

    print(f"POST {url}")
    print(f"  -> HTTP {status} in {elapsed_ms:.0f}ms")
    for key in ("status", "state", "task_key", "trace_id", "message", "detail", "reason"):
        if key in body:
            print(f"  {key}: {body[key]}")
    task_key = str(body.get("task_key") or f"{repo}#{args.pr_number}@{head_sha}")
    print("\n下一步：")
    print(f"  uv run python -m app.cli demo status  \"{task_key}\"")
    print(f"  uv run python -m app.cli demo approve \"{task_key}\"")

    # 202 与 200 都是"底座按预期工作"，只有 4xx/5xx 才算失败。
    return 0 if status in (200, 202) else 1


def cmd_demo_status(args: argparse.Namespace) -> int:
    identity = _identity_from_task_key(args.task_key)
    from apps.code_review_pipeline.storage.review_store import build_review_store

    store = build_review_store()
    state = store.get_task_state(identity)
    posted = store.get_posted_review_id(identity)
    print(f"task_key : {args.task_key}")
    print(f"identity : {identity}")
    print(f"state    : {state or '(任务行不存在——还没投递过?)'}")
    print(f"posted   : {posted or '(未写回)'}")
    return 0 if state else 1


def _summary_from_audit(task_key: str) -> str:
    """从审计链里还原一条人读得懂的结论摘要。

    为什么绕审计取：**逐条 finding 目前没有持久化**——``code_review_findings`` 这张
    表建了但全仓库没有任何写入方（只有 schema.sql 与一条外键注释提到它）。所以拿不
    到每条 finding 的 file/line。审计里存的是每个 agent 的**严重度计数**，那是被
    真正落盘、并且有哈希链保护的部分，就用它。

    这是一个已知的欠账而不是权宜：要让 PR 评论里有逐条定位，得先把 findings 写进
    ``code_review_findings``。本轮不补——它属于"结论存储"，不在可靠执行底座范围内。
    """
    from app.audit.recorder import entries_for_trace
    from app.config import settings
    import pathlib

    rows = entries_for_trace(pathlib.Path(settings.log_dir), task_key)
    counts: dict[str, int] = {}
    for row in rows:
        if row.get("agent_name") == "human_decision":
            continue
        for part in str(row.get("agent_output", "")).split(","):
            if "=" not in part:
                continue
            sev, _, num = part.partition("=")
            counts[sev.strip()] = counts.get(sev.strip(), 0) + int(num.strip() or 0)
    if not counts:
        return f"moa code review: `{task_key}`"
    breakdown = "，".join(f"{k} {v}" for k, v in sorted(counts.items()))
    return f"moa code review: `{task_key}`\n\n发现：{breakdown}。"


def cmd_demo_approve(args: argparse.Namespace) -> int:
    identity = _identity_from_task_key(args.task_key)

    async def run() -> str:
        from app.config import settings
        from apps.code_review_pipeline.approval import approve_task, reject_task
        from apps.code_review_pipeline.routing.github_provider import build_github_client
        from apps.code_review_pipeline.storage.review_store import build_review_store

        store = build_review_store()
        if args.reject:
            return await reject_task(
                store, identity, args.task_key,
                operator=args.operator, reason=args.reject,
            )
        github = build_github_client()
        try:
            # --real 才真发；默认与 settings 一致（dry-run）。dry-run 走的是**完全
            # 相同**的判定路径（主判据 -> marker 兜底），只在最后一步停下。
            dry_run = not args.real and settings.code_review_publish_dry_run
            outcome = await approve_task(
                store, github, identity, args.task_key,
                operator=args.operator,
                summary=_summary_from_audit(args.task_key),
                dry_run=dry_run,
            )
        finally:
            close = getattr(github, "aclose", None)
            if close is not None:
                await close()
        return outcome.status + (f" (review_id={outcome.review_id})" if outcome.review_id else "")

    print(f"task_key : {args.task_key}")
    print(f"result   : {asyncio.run(run())}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli", description="moa-gateway 操作入口"
    )
    sub = parser.add_subparsers(dest="group", required=True)
    demo = sub.add_parser("demo", help="golden path 演示命令")
    demo_sub = demo.add_subparsers(dest="command", required=True)

    collab = sub.add_parser(
        "collab", help="跑一次多 Agent 协作链路（ADR-022 / R6）"
    )
    collab.add_argument("task", nargs="?", default="写一个 Python 函数，然后审查这段代码，并且总结一下要点")
    collab.add_argument("--live", action="store_true", help="用已配置的 LLM 做规划与评审（会花钱）")
    collab.set_defaults(func=cmd_collab)

    def gateway_args(p: argparse.ArgumentParser) -> None:
        from app.config import settings

        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=settings.gateway_port)

    review = demo_sub.add_parser(
        "review", help="按真实 HMAC 签名投递一次 PR 审查 webhook（走真实 HTTP）"
    )
    review.add_argument("pr_number", type=int)
    review.add_argument("--repo", default=os.getenv("DEMO_REPO", "shing26/moa-gateway"))
    review.add_argument(
        "--sha",
        default=None,
        help="head sha；默认按 (repo, pr) 稳定推导，于是同一条命令连跑两次能验幂等",
    )
    review.add_argument(
        "--event",
        default="opened",
        choices=["opened", "synchronize", "reopened", "closed", "labeled"],
        help="GitHub 事件类型；非审查事件会返回 200 ignored 且不入队",
    )
    gateway_args(review)
    review.set_defaults(func=cmd_demo_review)

    status = demo_sub.add_parser("status", help="查任务行状态与写回 id")
    status.add_argument("task_key", help="owner/repo#<pr>@<sha>")
    status.set_defaults(func=cmd_demo_status)

    approve = demo_sub.add_parser("approve", help="人工批准（默认 dry-run）并幂等写回")
    approve.add_argument("task_key", help="owner/repo#<pr>@<sha>")
    approve.add_argument("--operator", default="cli-demo")
    approve.add_argument("--real", action="store_true", help="真发评论（默认受 dry-run 开关约束）")
    approve.add_argument("--reject", default="", metavar="REASON", help="拒绝而不是批准")
    approve.set_defaults(func=cmd_demo_approve)

    return parser


def cmd_collab(args: argparse.Namespace) -> int:
    """跑一次协作并打印摘要。完整执行轨迹看 ``scripts/run_collaboration.py``。

    两个入口分工：CLI 给"顺手看一眼"的人，脚本给想读 ``node_path`` 与每轮裁决的
    人。默认都是 mock（零网络零 token），``--live`` 才动真模型。
    """
    # Windows 控制台默认不是 UTF-8，中文产出会变乱码；只在本次命令里收口。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from app.orchestration.collaboration import (
        CollabOrchestrator,
        MockCollaborationLLM,
        ScriptedExpert,
    )

    if args.live:
        orchestrator = CollabOrchestrator.from_deps()
    else:
        orchestrator = CollabOrchestrator(
            collaboration_llm=MockCollaborationLLM(),
            experts={key: ScriptedExpert() for key in ("coder", "general", "review")},
        )

    async def run():
        return await orchestrator.run(task=args.task, session_id="collab-cli")

    result = asyncio.run(run())
    print(f"trace_id : {result.trace_id}")
    print(f"status   : {result.status}  need_human_review={result.need_human_review}")
    print(f"rounds   : {result.rounds}  guard_action={result.guard_action or '-'}")
    print(f"plan     : {[(i.index, i.agent) for i in result.plan]}")
    for verdict in result.critique_history:
        print(f"critic   : {verdict.decision} targets={list(verdict.targets)} {list(verdict.reasons)}")
    print(f"node_path: {' -> '.join(result.node_path)}")
    print("text     :")
    print(result.final_text or "(empty)")
    if result.need_human_review:
        print("（未通过人工确认，请勿当作成功交付）")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows 下 psycopg 拒绝 ProactorEventLoop（与 app/__main__.py 同一处理）。
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
