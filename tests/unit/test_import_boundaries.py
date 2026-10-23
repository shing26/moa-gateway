from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "imports",
    (
        "import bench.report; import bench.experiment",
        "import bench.experiment; import bench.report",
    ),
)
def test_report_and_experiment_packages_import_in_either_order(
    imports: str,
) -> None:
    result = subprocess.run(
        [sys.executable, "-c", imports],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
