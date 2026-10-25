"""变更管线 dry-run 演示（ADR-030）。

在一个临时 git 仓库里模拟 agent 改代码 → 跑验证 → 产出变更包。
默认 dry-run：不触碰任何远端。全程零网络、零 token。

    python scripts/run_change_demo.py            # 验证通过
    python scripts/run_change_demo.py --fail     # 验证失败，展示出口被挡住
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.code_review_pipeline.change import (  # noqa: E402
    ChangePipeline,
    ChangeRequest,
)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class DemoAgent:
    """把 add 里的 `a - b` 改成 `a + b`，模拟一次最小修复。"""

    async def run(self, *, workspace: Path, goal: str) -> str:
        target = workspace / "calc.py"
        target.write_text(
            target.read_text(encoding="utf-8").replace("a - b", "a + b"),
            encoding="utf-8",
        )
        return "patched calc.py"


def build_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "demo@example.com"], repo)
    _git(["config", "user.name", "Demo"], repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail", action="store_true", help="让验证失败以展示闸门")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="moa-change-") as raw:
        repo = build_repo(Path(raw))
        verify = (
            (sys.executable, "-c", "raise SystemExit(1)")
            if args.fail
            else (sys.executable, "-m", "pytest", "-q", "test_calc.py")
        )
        pipeline = ChangePipeline(
            github=object(),
            repo="demo/repo",
            workspace=repo,
            verify_command=verify,
            model_name="demo-model",
            push_enabled=False,
        )
        request = ChangeRequest(
            repo="demo/repo",
            base_sha=_git(["rev-parse", "HEAD"], repo),
            title="fix: add should add",
            body="agent detected `a - b` in add()",
            goal="fix add()",
            branch="moa/demo-fix",
            trace_id="demo-trace",
        )
        result = asyncio.run(pipeline.run(request, agent=DemoAgent()))

        print(f"status      : {result.status.value}")
        print(f"message     : {result.message}")
        if result.bundle:
            bundle = result.bundle
            print(f"files       : {[f.path for f in bundle.files]}")
            print(f"patch_hash  : {bundle.patch_hash[:16]}…")
            print(f"bundle_hash : {bundle.bundle_hash[:16]}…")
            print(f"verified    : {bundle.is_verified}")
            print("patch       :")
            print(bundle.patch)
        print(f"pr_url      : {result.pr_url or '(dry-run，未创建)'}")
        # dry-run 下"验证通过、包已生成"就是本演示的成功判据。
        return 0 if (result.bundle and result.bundle.is_verified) else 1


if __name__ == "__main__":
    raise SystemExit(main())
