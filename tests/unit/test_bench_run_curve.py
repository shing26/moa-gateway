"""一键曲线驱动（``bench/run_curve.py``）：纯编排，离线。

真跑曲线要花钱、要打 API，那是票 09 的人工验收。这里注入一个假 runner，把
"B1 主实验 → 由 B1 结果推 B3 复跑 → 聚合两份报告"这段决策逻辑演一遍：落盘命名、
B3 只挑 B1 的失败、无效任务剔除、报告档位正确。不碰网络、不碰 git。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import run_curve as cli
from bench.agent.types import AgentRun, RunResult, Usage
from bench.arms import arm_names
from bench.harness.runner import InvalidTaskError, TaskSpec


def _spec(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        base_commit="abc",
        verify_command=("python", "-m", "pytest"),
        test_paths=("tests/test_x.py",),
        test_patch=Path("x.test.patch"),
    )


def _result(
    spec: TaskSpec,
    arm: str,
    *,
    tier: str,
    outcome: str,
    cost: float,
    baseline: float,
) -> RunResult:
    run = AgentRun(
        status="completed",
        steps=(),
        usage=Usage(cost_usd=cost),
        model="m",
        temperature=0.0,
    )
    return RunResult(
        task_id=spec.task_id,
        arm=arm,
        base_commit=spec.base_commit,
        agent_run=run,
        outcome=outcome,  # type: ignore[arg-type]
        patch="",
        fail_first_exit_code=1,
        verdict_exit_code=0 if outcome == "pass" else 1,
        verdict_output="",
        wall_clock_s=0.0,
        budget_tier=tier,  # type: ignore[arg-type]
        baseline_cost_usd=baseline,
        source=spec.source,
    )


class FakeRunner:
    """把 (任务, 档位, 预算档) 映射到预设结局；记录每次调用，不碰真 LLM / git。

    ``outcomes`` 的键按优先级查：先 ``(task, arm, tier)``，再 ``(task, arm)``，都没有
    就默认 ``pass``。于是"某档在 B1 失败、放宽后 B3 通过"可以一条 dict 说清。
    """

    def __init__(
        self,
        outcomes: dict[tuple[str, ...], str] | None = None,
        *,
        invalid: frozenset[str] = frozenset(),
        cost: float = 0.1,
    ) -> None:
        self.outcomes = outcomes or {}
        self.invalid = set(invalid)
        self.cost = cost
        self.calls: list[tuple[str, str, str, float]] = []

    async def __call__(
        self,
        *,
        spec: TaskSpec,
        agent_factory: object,
        repo: Path,
        workdir: Path,
        arm: str,
        goal: str,
        budget: object,
        budget_tier: str,
        baseline_cost_usd: float,
    ) -> RunResult:
        self.calls.append(
            (spec.task_id, arm, budget_tier, getattr(budget, "max_cost_usd", 0.0))
        )
        if spec.task_id in self.invalid:
            raise InvalidTaskError(spec.task_id)
        outcome = (
            self.outcomes.get((spec.task_id, arm, budget_tier))
            or self.outcomes.get((spec.task_id, arm))
            or "pass"
        )
        return _result(
            spec,
            arm,
            tier=budget_tier,
            outcome=outcome,
            cost=self.cost,
            baseline=baseline_cost_usd,
        )


async def _run(
    tmp_path: Path,
    *,
    specs: list[TaskSpec],
    runner: FakeRunner,
    arms: list[str],
    repeats: int = 1,
) -> cli.CurveOutcome:
    return await cli.run_curve(
        specs=specs,
        arms=arms,
        repeats=repeats,
        repo=tmp_path,
        workdir=tmp_path / "work",
        b1_dir=tmp_path / "runs" / "b1",
        b3_dir=tmp_path / "runs" / "b3",
        factory_for=lambda arm: object(),
        runner=runner,
    )


@pytest.mark.asyncio
async def test_writes_one_json_per_run_and_names_them_deterministically(
    tmp_path: Path,
) -> None:
    specs = [_spec("t1"), _spec("t2")]
    out = await _run(
        tmp_path, specs=specs, runner=FakeRunner(), arms=["L0", "L3"], repeats=2
    )

    assert out.b1_runs == 8  # 2 任务 × 2 档 × 2 次
    assert sorted(p.name for p in out.b1_dir.glob("*.json")) == [
        "t1-L0-1.json",
        "t1-L0-2.json",
        "t1-L3-1.json",
        "t1-L3-2.json",
        "t2-L0-1.json",
        "t2-L0-2.json",
        "t2-L3-1.json",
        "t2-L3-2.json",
    ]
    # 全通过 → 没有 B1 失败，B3 不该凭空造 run。
    assert out.b3_runs == 0
    assert list(out.b3_dir.glob("*.json")) == []


@pytest.mark.asyncio
async def test_b3_only_reruns_b1_failures_and_carries_the_baseline(
    tmp_path: Path,
) -> None:
    specs = [_spec("t1"), _spec("t2")]
    runner = FakeRunner(
        {
            ("t1", "L3"): "fail",
            ("t1", "L3", "B3"): "pass",
            ("t2", "L2"): "budget_exhausted",
            ("t2", "L2", "B3"): "pass",
        },
        cost=0.4,
    )

    out = await _run(tmp_path, specs=specs, runner=runner, arms=["L0", "L2", "L3"])

    # B1 失败/超预算的两格各复跑一次；通过的两格不复跑。
    assert out.b3_runs == 2
    assert sorted(p.name for p in out.b3_dir.glob("*.json")) == [
        "t1-L3-1.json",
        "t2-L2-1.json",
    ]
    assert ("t1", "L0", "B3") not in [(t, a, tier) for t, a, tier, _ in runner.calls]

    # 基线取自 B1 同格的花费，且 B3 用的是放宽档的美元上限，不是 B1 的。
    b3_json = json.loads(
        (out.b3_dir / "t1-L3-1.json").read_text(encoding="utf-8")
    )
    assert b3_json["budget_tier"] == "B3"
    assert b3_json["baseline_cost_usd"] == pytest.approx(0.4)
    b3_budget = next(cost for _, _, tier, cost in runner.calls if tier == "B3")
    assert b3_budget > 1.5  # 放宽档，大于 B1 默认上限

    assert out.b1_report.tier == "B1"
    assert out.b3_report.tier == "B3"


@pytest.mark.asyncio
async def test_invalid_task_is_skipped_and_dropped_from_the_declared_set(
    tmp_path: Path,
) -> None:
    specs = [_spec("bad"), _spec("good")]
    runner = FakeRunner(invalid=frozenset({"bad"}))

    out = await _run(tmp_path, specs=specs, runner=runner, arms=["L0", "L3"], repeats=2)

    assert out.invalid_tasks == ("bad",)
    # bad 在第一次 arm/repeat 上被判无效后就不再浪费——只有 good 的 2×2 落盘。
    assert out.b1_runs == 4
    assert list(out.b1_dir.glob("bad-*.json")) == []
    # 报告声明的是剔除无效任务后的任务集：bad 不该以"缺口"形式留在表里。
    assert out.b1_report.tasks == ("good",)


@pytest.mark.asyncio
async def test_repeats_below_one_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await _run(
            tmp_path, specs=[_spec("t1")], runner=FakeRunner(), arms=["L0"], repeats=0
        )


def test_policy_applies_only_the_flags_given() -> None:
    base = cli._policy(cli._parse_args(["--tasks", "t1"]))
    assert (base.main_cost_usd, base.main_steps) == (1.5, 30)

    overridden = cli._policy(
        cli._parse_args(
            [
                "--tasks",
                "t1",
                "--main-cost-usd",
                "0.5",
                "--relaxed-steps",
                "9",
                "--max-tokens",
                "1234",
            ]
        )
    )
    assert overridden.main_cost_usd == 0.5
    assert overridden.relaxed_steps == 9
    assert overridden.max_tokens == 1234
    assert overridden.main_steps == 30  # 没给的就保留默认
    assert overridden.relaxed_cost_usd == base.relaxed_cost_usd


def test_unknown_arms_are_detected_against_the_registry() -> None:
    assert cli._unknown_arms(["L0", "L9", "L3"]) == ("L9",)
    assert cli._unknown_arms(arm_names()) == ()


@pytest.mark.asyncio
async def test_main_rejects_an_unknown_arm_before_spending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 未知 arm 必须在装配 LLM / 物化工作区之前就拦下——否则会白花钱。
    code = await cli.main(["--tasks", "988fccb", "--arms", "L9"])
    assert code == 4
    assert "L9" in caplog.text


def test_build_llm_honours_model_and_temperature_overrides() -> None:
    args = cli._parse_args(
        ["--tasks", "t1", "--model", "openrouter/anthropic/claude-x", "--temperature", "0.2"]
    )
    llm = cli._build_llm(args)
    assert llm.config.model == "openrouter/anthropic/claude-x"
    assert llm.config.temperature == 0.2


def test_cli_loads_the_project_env_before_building_the_llm() -> None:
    """CLI 必须走项目统一的 LLM 配置来源（app.config 的 load_dotenv 副作用）。"""
    import sys

    assert "app.config" in sys.modules
