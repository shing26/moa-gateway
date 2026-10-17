"""手动 CLI（``bench/run_task.py``）的参数解析与 LLM 装配。

``main`` 本身不进 CI——它真花钱、真打 API，那是 §5.0 的人工验收。这里守的是
"参数契约"与"模型装配"这两段纯函数，免得改错一个默认值都没人发现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench import run_task as cli
from bench.arms import DEFAULT_ARM, arm_names

ROOT = Path(__file__).resolve().parents[2]


def test_parse_args_defaults() -> None:
    args = cli.parse_args(["--task", "988fccb"])
    assert args.task == "988fccb"
    assert args.arm == "L0"
    # 预算由 --budget-tier 的策略给默认值，--max-* 是显式覆盖（默认不覆盖）。
    assert args.budget_tier == "B1"
    assert args.baseline_cost_usd == 0.0
    assert args.max_steps is None
    assert args.max_cost_usd is None
    assert args.max_tokens is None
    assert args.out == ""
    assert args.model == ""
    assert args.temperature is None
    assert args.tasks_dir == str(ROOT / "bench" / "tasks")
    assert args.workdir == str(ROOT / ".bench-work")


def test_resolve_budget_uses_the_tier_policy_and_honours_overrides() -> None:
    b1 = cli._resolve_budget(cli.parse_args(["--task", "x"]))
    assert (b1.max_cost_usd, b1.max_steps) == (1.5, 30)

    b3 = cli._resolve_budget(cli.parse_args(["--task", "x", "--budget-tier", "B3"]))
    assert b3.max_cost_usd > b1.max_cost_usd
    assert b3.max_steps > b1.max_steps

    overridden = cli._resolve_budget(
        cli.parse_args(
            [
                "--task",
                "x",
                "--budget-tier",
                "B3",
                "--max-steps",
                "7",
                "--max-cost-usd",
                "0.25",
                "--max-tokens",
                "1234",
            ]
        )
    )
    assert (overridden.max_cost_usd, overridden.max_steps) == (0.25, 7)
    assert overridden.max_tokens == 1234


def test_parse_args_honours_overrides() -> None:
    args = cli.parse_args(
        ["--task", "x", "--arm", "L3", "--max-steps", "7", "--max-cost-usd", "0.25"]
    )
    assert (args.arm, args.max_steps, args.max_cost_usd) == ("L3", 7, 0.25)


def test_parse_args_requires_a_task() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args([])


def test_default_arm_is_a_registered_arm() -> None:
    """默认档位必须真能取到——默认值与"哪些可用"只能有一个来源。"""
    assert cli.parse_args(["--task", "x"]).arm == DEFAULT_ARM
    assert DEFAULT_ARM in arm_names()


def test_cli_help_lists_every_registered_arm(capsys: pytest.CaptureFixture[str]) -> None:
    """注册表一改，--arm 的说明跟着改：CLI 不自己维护一份可用清单。"""
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])
    printed = capsys.readouterr().out
    assert all(name in printed for name in arm_names())


@pytest.mark.asyncio
async def test_main_rejects_an_unknown_arm(caplog: pytest.LogCaptureFixture) -> None:
    """未知 arm 要报错，而不是静默按 L0 跑——否则曲线上的 L3 可能是 L0。"""
    code = await cli.main(["--task", "988fccb", "--arm", "L9"])
    assert code != 0
    assert "L9" in caplog.text


def test_build_llm_honours_model_and_temperature_overrides() -> None:
    args = cli.parse_args(
        ["--task", "x", "--model", "openrouter/anthropic/claude-x", "--temperature", "0.2"]
    )
    llm = cli.build_llm(args)
    # 覆盖发生在 from_env 之后，所以记录的就是传进来的字面值。
    assert llm.config.model == "openrouter/anthropic/claude-x"
    assert llm.config.temperature == 0.2


def test_build_llm_falls_back_to_env_defaults() -> None:
    llm = cli.build_llm(cli.parse_args(["--task", "x"]))
    assert llm.config.model  # from_env 恒有默认值（缺省 gpt-4o-mini）
    assert 0.0 <= llm.config.temperature <= 2.0


def test_cli_loads_the_project_env_before_building_the_llm() -> None:
    """CLI 必须走项目统一的 LLM 配置来源（app.config 的 load_dotenv 副作用）。

    不加载 .env 的话 ``LLMConfig.from_env`` 退回默认值，拿 gpt-4o-mini 去连
    api.openai.com——这正是 09 人工验收第一次跑挂的原因。
    """
    import sys

    assert "app.config" in sys.modules
