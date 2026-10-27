from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_product_core_does_not_import_bench() -> None:
    offenders: list[str] = []
    for path in (ROOT / "apps" / "code_review_pipeline" / "delivery").rglob("*.py"):
        if path.name == "bench_agent.py" or "bench" in path.parts:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith(("import bench", "from bench")):
                offenders.append(f"{path.relative_to(ROOT)}:{line_no}")
    assert offenders == []


def test_flow_does_not_import_concrete_github_or_llm_clients() -> None:
    text = (
        ROOT
        / "apps"
        / "code_review_pipeline"
        / "delivery"
        / "flow.py"
    ).read_text(encoding="utf-8")
    assert "GitHubClient" not in text
    assert "LLMClient" not in text
    assert "ChangePipeline" not in text
