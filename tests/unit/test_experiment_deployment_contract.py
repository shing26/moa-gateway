from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_compose_mounts_experiment_artifacts_read_only() -> None:
    compose = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    gateway = compose["services"]["gateway"]

    assert "BENCH_EXPERIMENTS_DIR=/app/.bench-experiments" in gateway["environment"]
    assert "./.bench-experiments:/app/.bench-experiments:ro" in gateway["volumes"]


def test_docker_context_excludes_live_experiment_artifacts() -> None:
    ignored = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".bench-experiments" in ignored
