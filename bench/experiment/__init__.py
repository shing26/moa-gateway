"""可验证多智能体实验台：统一 Run Record 与执行外壳。"""

from bench.experiment.adapters import (
    BenchExperimentAdapter,
    CollaborationExperimentAdapter,
)
from bench.experiment.protocol import (
    DEFAULT_PROTOCOL_PATH,
    ExperimentProtocol,
    load_protocol,
    protocol_from_dict,
)
from bench.experiment.runner import ExperimentRunner
from bench.experiment.store import ExperimentStore
from bench.experiment.types import (
    ExperimentManifest,
    ExperimentOutcome,
    ExperimentSpec,
    RunMetrics,
    RunRecord,
    TrialEvidence,
    TrialExecutor,
    WorkspaceSnapshot,
    compare_run_records,
    fingerprint_workspace,
)
from bench.experiment.batch import (
    BATCH_SCHEMA_VERSION,
    BatchCell,
    BatchManifest,
    BatchOutcome,
    BatchReport,
    BatchRunner,
    BatchStore,
    build_batch_report,
)
from bench.experiment.cross_model import (
    CrossModelReport,
    build_cross_model_report,
    write_cross_model_report,
)

__all__ = [
    "BATCH_SCHEMA_VERSION",
    "BenchExperimentAdapter",
    "BatchCell",
    "BatchManifest",
    "BatchOutcome",
    "BatchReport",
    "BatchRunner",
    "BatchStore",
    "CollaborationExperimentAdapter",
    "CrossModelReport",
    "DEFAULT_PROTOCOL_PATH",
    "ExperimentManifest",
    "ExperimentOutcome",
    "ExperimentProtocol",
    "ExperimentRunner",
    "ExperimentSpec",
    "ExperimentStore",
    "RunMetrics",
    "RunRecord",
    "TrialEvidence",
    "TrialExecutor",
    "WorkspaceSnapshot",
    "build_batch_report",
    "build_cross_model_report",
    "compare_run_records",
    "fingerprint_workspace",
    "load_protocol",
    "protocol_from_dict",
    "write_cross_model_report",
]
