"""校验审计 WAL 的哈希链——ADR-019 那条回路的**末端入口**。

用法::

    uv run python scripts/verify_audit_chain.py                    # 校验 logs/ 下全部文件
    uv run python scripts/verify_audit_chain.py --limit 3          # 只校验最新 3 个
    uv run python scripts/verify_audit_chain.py --logs-dir logs

退出码：``0`` 全部完整；``1`` 有文件断裂（打印文件名与首个断裂行号）；``2`` 参数/IO 错误。

为什么需要这个脚本：``verify_chain`` 此前**没有任何消费者**——链写出来了却没人跑，
"篡改可发现"因此没有回路末端。按 `CONTEXT.md` 对闭环的定义（末端有没有人消费产出），
那不算闭环。这里给出两个末端：

* 本脚本：按需/定时全量校验；
* ``/healthz``：只校验**最新一个**文件（代价封顶，不挂在请求路径上做全量 IO）。

注意两处刻意的边界（详见 ``verify_chain`` 的 docstring）：每个日文件是**独立的一条链**；
史前条目（本功能之前的旧行没有哈希字段）会被**如实报成断裂**——"无法证明连续"应当可见。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

# 直接跑脚本时把仓库根放进 sys.path，否则 `python scripts/x.py` 找不到 app 包
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.audit.wal import verify_audit_dir  # noqa: E402


def _report_task(
    logs_dir: pathlib.Path, task_id: str, expect: int | None, quiet: bool
) -> int:
    """按 task_id 打印审计行，并校验条数与种类。

    种类校验是刻意加的：只数"7 条"的话，5 条 lifecycle + 2 条 agent 也能凑够 7，
    于是"某个 agent 没跑"这类缺陷就查不出来。
    """
    from app.audit.recorder import entries_for_trace
    from apps.code_review_pipeline.task_audit import (
        AGENT_NAMES,
        HUMAN_DECISION_AGENT,
        LIFECYCLE_AGENT,
    )

    rows = entries_for_trace(logs_dir, task_id)
    if not rows:
        print(f"task_id={task_id} 在 {logs_dir} 下没有任何审计行", file=sys.stderr)
        return 1

    agents = [r for r in rows if r.get("agent_name") in AGENT_NAMES]
    lifecycle = [r for r in rows if r.get("agent_name") == LIFECYCLE_AGENT]
    human = [r for r in rows if r.get("agent_name") == HUMAN_DECISION_AGENT]

    if not quiet:
        for row in rows:
            print(f"  {row.get('agent_name')}: {str(row.get('agent_output', ''))[:60]}")
    print(
        f"task_id={task_id}: 共 {len(rows)} 行"
        f"（agent {len(agents)}/5，lifecycle {len(lifecycle)}，人工决策 {len(human)}）"
    )

    missing = [a for a in AGENT_NAMES if a not in {r.get("agent_name") for r in agents}]
    if missing:
        print(f"缺少 agent 审计行: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not lifecycle:
        print("缺少任务 lifecycle 审计行", file=sys.stderr)
        return 1
    if expect is not None and len(rows) != expect:
        print(f"行数不符：期望 {expect}，实际 {len(rows)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Verify the tamper-evident audit hash chain")
    parser.add_argument("--logs-dir", type=pathlib.Path, default=root / "logs")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="只校验最新的 N 个文件（默认全部）",
    )
    parser.add_argument("--quiet", action="store_true", help="只打印结论与断裂点")
    parser.add_argument(
        "--task",
        default=None,
        help="只查某个 task_id 的审计行（D4 判据：5 agent + 1 lifecycle + 1 人工决策）",
    )
    parser.add_argument(
        "--expect-rows",
        type=int,
        default=None,
        help="配合 --task：期望的行数，不符则退出码 1",
    )
    args = parser.parse_args(argv)

    if not args.logs_dir.exists():
        print(f"logs 目录不存在: {args.logs_dir}", file=sys.stderr)
        return 2

    if args.task:
        return _report_task(args.logs_dir, args.task, args.expect_rows, args.quiet)

    results = verify_audit_dir(args.logs_dir, limit=args.limit)
    if not results:
        print(f"没有可校验的审计文件（{args.logs_dir}/audit-*.jsonl）")
        return 0

    broken = [
        (name, line) for name, (line, _) in sorted(results.items()) if line is not None
    ]
    unchained = [name for name, (_, chained) in sorted(results.items()) if chained == 0]
    if not args.quiet:
        for name, (line, chained) in sorted(results.items()):
            if line is not None:
                print(f"  {name}: 断裂于第 {line} 行")
            elif chained == 0:
                print(f"  {name}: 无链可校验（链上线前的数据）")
            else:
                print(f"  {name}: 完整（{chained} 行带链）")
    if broken:
        name, line = broken[0]
        print(
            f"审计链断裂：{len(broken)}/{len(results)} 个文件有问题，"
            f"首个是 {name} 第 {line} 行（该行起的内容无法证明未被改动）",
            file=sys.stderr,
        )
        return 1
    ok = len(results) - len(unchained)
    summary = f"审计链完整：{ok}/{len(results)} 个文件"
    if unchained:
        summary += f"；另有 {len(unchained)} 个文件无链可校验（链上线前的数据，非「完整」）"
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
