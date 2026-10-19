"""兼容入口：子进程执行已提升到 ``app.agents.command_runner``。"""

from app.agents.command_runner import CommandResult, build_env, run_command

__all__ = ["CommandResult", "build_env", "run_command"]
