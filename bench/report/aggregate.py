"""剂量曲线聚合器（§5.4）：一次 run 一份 JSON 是原料，结论是那张表。

输入是一批 ``RunResult`` JSON（``bench/run_task.py`` 落的盘），输出是 §5.4 的
**任务 × 档位**表：每一格是通过率与成本，每一列减去前一列就是"每加一个 agent 的
边际收益"。聚合只读落盘物、只做确定性算术——**不接 API、不依赖网络**，同一批输入
重算结果逐字相同。

三种"没通过"必须分开
--------------------
§8 把 ``fail``（验证没过，是判决）、``budget_exhausted``（太贵，是 B1 的核心信号）、
``error``（API/超时，是噪音）分成三类，混算会让"协作太贵"被误读成"协作不行"。
所以每格按结局分别计数，报告里它们**单独成列**，通过率只看 ``pass``。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: 四档的规范顺序——边际收益是"后一列减前一列"，顺序错了结论就反了。
ARM_ORDER: tuple[str, ...] = ("L0", "L1", "L2", "L3")

#: 判决/自报的四种结局（§8）。报告按它们分列，不与 ``fail`` 混算。
OUTCOMES: tuple[str, ...] = ("pass", "fail", "budget_exhausted", "error")


def _arm_rank(name: str) -> tuple[int, str]:
    """规范档位排前面（按 ``ARM_ORDER``），未登记的档位按字母排在后面。"""
    try:
        return (ARM_ORDER.index(name), "")
    except ValueError:
        return (len(ARM_ORDER), name)


@dataclass(frozen=True)
class RunRow:
    """一次 run 里聚合真正要看的字段。从 ``RunResult`` JSON 解析而来。"""

    task_id: str
    arm: str
    outcome: str
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    budget_tier: str = "B1"
    model: str = ""
    wall_clock_s: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunRow":
        agent_run = payload.get("agent_run") or {}
        usage = (agent_run.get("usage") or {}) if isinstance(agent_run, Mapping) else {}
        return cls(
            task_id=str(payload.get("task_id", "")),
            arm=str(payload.get("arm", "")),
            outcome=str(payload.get("outcome", "")),
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            budget_tier=str(payload.get("budget_tier", "B1") or "B1"),
            model=str(agent_run.get("model", "") if isinstance(agent_run, Mapping) else ""),
            wall_clock_s=float(payload.get("wall_clock_s", 0.0) or 0.0),
        )


def load_rows(paths: Iterable[Path]) -> tuple[RunRow, ...]:
    """读一批 ``RunResult`` JSON。按传入顺序保留——报告层不做隐式排序。"""
    return tuple(
        RunRow.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        for path in paths
    )


def load_rows_dir(directory: Path) -> tuple[RunRow, ...]:
    """读一个目录下的全部 ``*.json``。按文件名排序，保证重算结果确定。"""
    root = Path(directory)
    return load_rows(sorted(root.glob("*.json")))


@dataclass(frozen=True)
class CellResult:
    """一格：某个 (任务, 档位) 上跑过的全部 run 的分布与成本。"""

    task_id: str
    arm: str
    passes: int = 0
    fails: int = 0
    budget_exhausted: int = 0
    errors: int = 0
    total_cost_usd: float = 0.0

    @property
    def runs(self) -> int:
        return self.passes + self.fails + self.budget_exhausted + self.errors

    @property
    def pass_rate(self) -> float | None:
        """通过率；样本为 0（缺口）时是 ``None``，不是 0——两者含义不同。"""
        return self.passes / self.runs if self.runs else None

    @property
    def avg_cost_usd(self) -> float | None:
        return self.total_cost_usd / self.runs if self.runs else None

    @property
    def outcomes(self) -> dict[str, int]:
        return {
            "pass": self.passes,
            "fail": self.fails,
            "budget_exhausted": self.budget_exhausted,
            "error": self.errors,
        }


@dataclass(frozen=True)
class ArmColumn:
    """一列：某档位跨全部任务的合计。"""

    arm: str
    runs: int = 0
    passes: int = 0
    fails: int = 0
    budget_exhausted: int = 0
    errors: int = 0
    total_cost_usd: float = 0.0
    tasks: int = 0

    @property
    def pass_rate(self) -> float | None:
        return self.passes / self.runs if self.runs else None

    @property
    def avg_cost_usd(self) -> float | None:
        return self.total_cost_usd / self.runs if self.runs else None

    @property
    def cost_per_pass_usd(self) -> float | None:
        """每通过一次的美元成本；一次都没通过时是 ``None``（不是 0）。"""
        return self.total_cost_usd / self.passes if self.passes else None

    @property
    def outcomes(self) -> dict[str, int]:
        return {
            "pass": self.passes,
            "fail": self.fails,
            "budget_exhausted": self.budget_exhausted,
            "error": self.errors,
        }


@dataclass(frozen=True)
class MarginalStep:
    """相邻两档的边际收益：后一列减前一列。任一侧是缺口则记 ``None``。"""

    arm: str
    previous_arm: str
    delta_pass_rate: float | None
    delta_avg_cost_usd: float | None


@dataclass(frozen=True)
class DoseReport:
    """一次聚合的结论。``to_dict`` 只吐确定性的数字，重算逐字相同。"""

    tier: str
    arms: tuple[str, ...]
    tasks: tuple[str, ...]
    cells: tuple[CellResult, ...]
    columns: tuple[ArmColumn, ...]
    marginals: tuple[MarginalStep, ...]
    total_runs: int

    def cell(self, task_id: str, arm: str) -> CellResult:
        for item in self.cells:
            if item.task_id == task_id and item.arm == arm:
                return item
        raise KeyError((task_id, arm))

    @property
    def gaps(self) -> tuple[tuple[str, str], ...]:
        """样本为 0 的 (任务, 档位) 组合——报告必须把这些缺口标出来。"""
        return tuple(
            (item.task_id, item.arm) for item in self.cells if item.runs == 0
        )

    @property
    def max_runs_per_cell(self) -> int:
        return max((item.runs for item in self.cells), default=0)

    @property
    def min_nonzero_runs_per_cell(self) -> int:
        counts = [item.runs for item in self.cells if item.runs > 0]
        return min(counts, default=0)

    @property
    def is_single_sample(self) -> bool:
        """每格最多一次运行：这是单点样本，不能当曲线读（§9）。"""
        return self.max_runs_per_cell <= 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "arms": list(self.arms),
            "tasks": list(self.tasks),
            "total_runs": self.total_runs,
            "max_runs_per_cell": self.max_runs_per_cell,
            "min_nonzero_runs_per_cell": self.min_nonzero_runs_per_cell,
            "is_single_sample": self.is_single_sample,
            "cells": [
                {
                    **asdict(item),
                    "runs": item.runs,
                    "pass_rate": item.pass_rate,
                    "avg_cost_usd": item.avg_cost_usd,
                }
                for item in self.cells
            ],
            "columns": [
                {
                    **asdict(item),
                    "pass_rate": item.pass_rate,
                    "avg_cost_usd": item.avg_cost_usd,
                    "cost_per_pass_usd": item.cost_per_pass_usd,
                }
                for item in self.columns
            ],
            "marginals": [asdict(item) for item in self.marginals],
            "gaps": [list(gap) for gap in self.gaps],
        }


def _aggregate_cell(rows: Sequence[RunRow], task_id: str, arm: str) -> CellResult:
    counts = dict.fromkeys(OUTCOMES, 0)
    cost = 0.0
    for row in rows:
        if row.task_id != task_id or row.arm != arm:
            continue
        if row.outcome in counts:
            counts[row.outcome] += 1
        cost += row.cost_usd
    return CellResult(
        task_id=task_id,
        arm=arm,
        passes=counts["pass"],
        fails=counts["fail"],
        budget_exhausted=counts["budget_exhausted"],
        errors=counts["error"],
        total_cost_usd=cost,
    )


def _aggregate_column(rows: Sequence[RunRow], arm: str) -> ArmColumn:
    chosen = [row for row in rows if row.arm == arm]
    counts = dict.fromkeys(OUTCOMES, 0)
    for row in chosen:
        if row.outcome in counts:
            counts[row.outcome] += 1
    return ArmColumn(
        arm=arm,
        runs=len(chosen),
        passes=counts["pass"],
        fails=counts["fail"],
        budget_exhausted=counts["budget_exhausted"],
        errors=counts["error"],
        total_cost_usd=sum(row.cost_usd for row in chosen),
        tasks=len({row.task_id for row in chosen}),
    )


def _delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def build_report(rows: Iterable[RunRow], *, tier: str = "B1") -> DoseReport:
    """把一批 run 聚成 ``tier`` 档的剂量曲线报告。"""
    selected = [row for row in rows if row.budget_tier == tier]

    present_arms = {row.arm for row in selected}
    # 规范档位即使一个 run 都没有也要出现在表里——缺了就是缺口，不是"这档不存在"。
    arms = tuple(sorted(present_arms | set(ARM_ORDER), key=_arm_rank))
    tasks = tuple(sorted({row.task_id for row in selected}))

    cells = tuple(
        _aggregate_cell(selected, task_id, arm) for task_id in tasks for arm in arms
    )
    columns = tuple(_aggregate_column(selected, arm) for arm in arms)
    marginals = tuple(
        MarginalStep(
            arm=columns[index].arm,
            previous_arm=columns[index - 1].arm,
            delta_pass_rate=_delta(
                columns[index].pass_rate, columns[index - 1].pass_rate
            ),
            delta_avg_cost_usd=_delta(
                columns[index].avg_cost_usd, columns[index - 1].avg_cost_usd
            ),
        )
        for index in range(1, len(columns))
    )
    return DoseReport(
        tier=tier,
        arms=arms,
        tasks=tasks,
        cells=cells,
        columns=columns,
        marginals=marginals,
        total_runs=len(selected),
    )


def _fmt_rate(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.0f}%"


def _fmt_cost(cost: float | None) -> str:
    return "—" if cost is None else f"${cost:.4f}"


def render_markdown(report: DoseReport) -> str:
    """人读版报告。数字全部来自 ``DoseReport``，不做二次计算。"""
    lines: list[str] = []
    lines.append(f"## 剂量曲线 · 预算档 {report.tier}")
    lines.append("")
    lines.append(f"- 运行总数：**{report.total_runs}**")
    lines.append(
        f"- 每格样本：最多 **{report.max_runs_per_cell}** 次、"
        f"非空格最少 **{report.min_nonzero_runs_per_cell}** 次"
    )
    if report.is_single_sample:
        lines.append(
            "- **注意：每个格子最多一次运行**——这是单点样本，是猜测不是曲线（§9）。"
        )
    if report.gaps:
        listed = "、".join(f"{task}×{arm}" for task, arm in report.gaps)
        lines.append(f"- 缺口（{len(report.gaps)} 格无样本）：{listed}")
    lines.append("")

    # ① 主表：任务 × 档位，每格是通过率与平均成本。
    lines.append("### 通过率 / 平均成本")
    lines.append("")
    header = "| 任务 | " + " | ".join(report.arms) + " |"
    divider = "| --- | " + " | ".join("---" for _ in report.arms) + " |"
    lines.append(header)
    lines.append(divider)
    for task_id in report.tasks:
        cells = [report.cell(task_id, arm) for arm in report.arms]
        rendered = " | ".join(
            f"{_fmt_rate(cell.pass_rate)} / {_fmt_cost(cell.avg_cost_usd)}"
            if cell.runs
            else "—（缺口）"
            for cell in cells
        )
        lines.append(f"| {task_id} | {rendered} |")
    totals = " | ".join(
        f"{_fmt_rate(column.pass_rate)} / {_fmt_cost(column.avg_cost_usd)}"
        for column in report.columns
    )
    lines.append(f"| **全部** | {totals} |")
    lines.append("")

    # ② 结局分布：pass / fail / budget_exhausted / error 单独成列，不混算。
    lines.append("### 结局分布（各自成列，不与 fail 混算）")
    lines.append("")
    lines.append("| 档位 | runs | pass | fail | budget_exhausted | error | 总成本 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for column in report.columns:
        lines.append(
            f"| {column.arm} | {column.runs} | {column.passes} | {column.fails} | "
            f"{column.budget_exhausted} | {column.errors} | "
            f"{_fmt_cost(column.total_cost_usd)} |"
        )
    lines.append("")

    # ③ 边际收益：后一列减前一列。
    lines.append("### 边际收益（后一档减前一档）")
    lines.append("")
    for step in report.marginals:
        delta_rate = (
            "—"
            if step.delta_pass_rate is None
            else f"{step.delta_pass_rate * 100:+.0f}pp"
        )
        delta_cost = (
            "—"
            if step.delta_avg_cost_usd is None
            else f"${step.delta_avg_cost_usd:+.4f}"
        )
        lines.append(
            f"- **{step.arm} - {step.previous_arm}**：通过率 {delta_rate}、"
            f"平均成本 {delta_cost}"
        )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "ARM_ORDER",
    "OUTCOMES",
    "ArmColumn",
    "CellResult",
    "DoseReport",
    "MarginalStep",
    "RunRow",
    "build_report",
    "load_rows",
    "load_rows_dir",
    "render_markdown",
]
