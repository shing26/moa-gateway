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
from bench.report.statistics import (
    ModelArmSummary,
    PairedComparison,
    PairedTask,
    model_arm_matrix,
    paired_pass_rate_comparison,
    render_paired_markdown,
)

__all__ = [
    "ARM_ORDER",
    "MIN_RUNS_PER_CELL",
    "OUTCOMES",
    "ArmColumn",
    "CellResult",
    "DoseReport",
    "MarginalStep",
    "ModelArmSummary",
    "OutcomeCounts",
    "PairedComparison",
    "PairedTask",
    "RunRow",
    "build_report",
    "load_rows",
    "load_rows_dir",
    "model_arm_matrix",
    "paired_pass_rate_comparison",
    "render_paired_markdown",
    "render_markdown",
]
