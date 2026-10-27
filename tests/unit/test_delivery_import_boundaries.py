from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ROOTS = (
    ROOT / "apps" / "code_review_pipeline" / "delivery",
    ROOT / "app" / "agents",
)


def test_product_core_does_not_import_bench() -> None:
    offenders: list[str] = []
    for product_root in PRODUCT_ROOTS:
        for path in product_root.rglob("*.py"):
            relative = path.relative_to(ROOT)
            if relative.parts and relative.parts[0] == "bench":
                continue
            for line_no, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                1,
            ):
                if line.strip().startswith(("import bench", "from bench")):
                    offenders.append(f"{relative}:{line_no}")
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
