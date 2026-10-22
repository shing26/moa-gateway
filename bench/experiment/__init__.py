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

__all__ = [
    "BenchExperimentAdapter",
    "CollaborationExperimentAdapter",
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
    "compare_run_records",
    "fingerprint_workspace",
    "load_protocol",
    "protocol_from_dict",
]
