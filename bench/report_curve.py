"""从落盘 JSON 离线重算剂量曲线与报告（票 08）。

    .venv\\Scripts\\python.exe bench/report_curve.py --runs .bench-work --tier B1

**不接 API、不依赖网络**：输入是 ``bench/run_task.py`` 落的 ``RunResult`` JSON，
输出是 §5.4 的表。同一批输入重算结果逐字相同，报告里的每个数字都能追回具体的 run。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.harness.budget import B1_TIER, B3_TIER  # noqa: E402
from bench.report import (  # noqa: E402
    RunRow,
    build_report,
    load_rows,
    load_rows_dir,
    render_markdown,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 RunResult JSON 聚合剂量曲线（离线、不花钱）"
    )
    parser.add_argument(
        "--runs",
        nargs="+",
        required=True,
        help="RunResult JSON 文件或包含它们的目录（可给多个）",
    )
    parser.add_argument("--tier", choices=[B1_TIER, B3_TIER], default=B1_TIER)
    parser.add_argument("--out", default="", help="把聚合结果另存为 JSON 的路径")
    return parser.parse_args(argv)


def _collect(paths: list[str]) -> tuple[RunRow, ...]:
    """按给定顺序读入，目录按文件名排序——重算结果因此确定。"""
    rows: list[RunRow] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            rows.extend(load_rows_dir(path))
        else:
            rows.extend(load_rows([path]))
    return tuple(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = _collect(args.runs)
    report = build_report(rows, tier=args.tier)

    print(render_markdown(report))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"聚合结果已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
