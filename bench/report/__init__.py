"""把一批 ``RunResult`` JSON 聚合成剂量曲线报告（§5.4 / 票 08）。"""

from bench.report.aggregate import (
    ARM_ORDER,
    MIN_RUNS_PER_CELL,
    OUTCOMES,
    ArmColumn,
    CellResult,
    DoseReport,
    MarginalStep,
    OutcomeCounts,
    RunRow,
    build_report,
    load_rows,
    load_rows_dir,
    render_markdown,
)

__all__ = [
    "ARM_ORDER",
    "MIN_RUNS_PER_CELL",
    "OUTCOMES",
    "ArmColumn",
    "CellResult",
    "DoseReport",
    "MarginalStep",
    "OutcomeCounts",
    "RunRow",
    "build_report",
    "load_rows",
    "load_rows_dir",
    "render_markdown",
]
