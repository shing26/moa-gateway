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


def main(argv: list[str] | None = None) -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Verify the tamper-evident audit hash chain")
    parser.add_argument("--logs-dir", type=pathlib.Path, default=root / "logs")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="只校验最新的 N 个文件（默认全部）",
    )
    parser.add_argument("--quiet", action="store_true", help="只打印结论与断裂点")
    args = parser.parse_args(argv)

    if not args.logs_dir.exists():
        print(f"logs 目录不存在: {args.logs_dir}", file=sys.stderr)
        return 2

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
