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
from bench.experiment.capacity import (
    CAPACITY_GATE_SUCCESSES,
    CapacityGateResult,
    CapacityPilotObservation,
    evaluate_capacity_gate,
    load_capacity_gate,
    observe_capacity_pilot,
)
from bench.experiment.pilot_runtime import (
    PilotRuntimeGate,
    load_pilot_runtime_gate,
)

__all__ = [
    "BATCH_SCHEMA_VERSION",
    "CAPACITY_GATE_SUCCESSES",
    "BenchExperimentAdapter",
    "BatchCell",
    "BatchManifest",
    "BatchOutcome",
    "BatchReport",
    "BatchRunner",
    "BatchStore",
    "CollaborationExperimentAdapter",
    "CapacityGateResult",
    "CapacityPilotObservation",
    "CrossModelReport",
    "DEFAULT_PROTOCOL_PATH",
    "ExperimentManifest",
    "ExperimentOutcome",
    "ExperimentProtocol",
    "ExperimentRunner",
    "ExperimentSpec",
    "ExperimentStore",
    "PilotRuntimeGate",
    "RunMetrics",
    "RunRecord",
    "TrialEvidence",
    "TrialExecutor",
    "WorkspaceSnapshot",
    "build_batch_report",
    "build_cross_model_report",
    "compare_run_records",
    "evaluate_capacity_gate",
    "fingerprint_workspace",
    "load_protocol",
    "load_capacity_gate",
    "load_pilot_runtime_gate",
    "observe_capacity_pilot",
    "protocol_from_dict",
    "write_cross_model_report",
]
