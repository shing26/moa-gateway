"""手动 CLI（``bench/run_task.py``）的参数解析与 LLM 装配。

``main`` 本身不进 CI——它真花钱、真打 API，那是 §5.0 的人工验收。这里守的是
"参数契约"与"模型装配"这两段纯函数，免得改错一个默认值都没人发现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench import run_task as cli

ROOT = Path(__file__).resolve().parents[2]


def test_parse_args_defaults() -> None:
    args = cli.parse_args(["--task", "988fccb"])
    assert args.task == "988fccb"
    assert args.arm == "L0"
    assert args.max_steps == 30
    assert args.max_cost_usd == 1.5
    assert args.out == ""
    assert args.model == ""
    assert args.temperature is None
    assert args.tasks_dir == str(ROOT / "bench" / "tasks")
    assert args.workdir == str(ROOT / ".bench-work")


def test_parse_args_honours_overrides() -> None:
    args = cli.parse_args(
        ["--task", "x", "--arm", "L3", "--max-steps", "7", "--max-cost-usd", "0.25"]
    )
    assert (args.arm, args.max_steps, args.max_cost_usd) == ("L3", 7, 0.25)


def test_parse_args_requires_a_task() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args([])


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
