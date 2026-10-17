"""arm 注册表的类型与查找。

§4.2 把四档编排划到 ``bench/arms/``，§5 要求四档"实现同一个接口、唯一变量是编排
拓扑"。注册表就是那个接口的落点：CLI 与测试都按名字从这里取档，而不是各自
if/else 一把。未登记的名字一律报错——静默退回 L0 会让"曲线上的 L3 其实是 L0"
这种事故无声通过。

**"哪些 arm 可用"的单一来源是 ``bench/arms/__init__.py`` 里的 ``ARMS``**；本模块
只提供类型、查找语义与"把 arm 变成 runner 要的工厂"这一步。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.tools import ToolRegistry

from bench.agent.types import AgentFactory, CodingAgent


class UnknownArmError(LookupError):
    """请求了注册表里没有的档位名。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"未知 arm: {name!r}")
        self.name = name


@dataclass(frozen=True)
class Arm:
    """一个档位：名字、一句话说明，以及"怎么把它造出来"。"""

    name: str
    description: str
    #: 造一个 arm 需要的全部依赖。构造期带 workspace 是为 L1–L3 留的统一形状——L0
    #: 用不上，它把 workspace 交给 ``CodingAgent.run()``。
    build: AgentFactory

    def factory(self, *, llm: Any) -> AgentFactory:
        """绑定 LLM，返回 runner 要的工厂；workspace / tools 由 runner 在调用时给。"""

        def factory(*, workspace: Path, tools: ToolRegistry) -> CodingAgent:
            return self.build(llm=llm, workspace=workspace, tools=tools)

        return factory


class ArmRegistry:
    """按名字查档位。实例由 ``bench/arms/__init__.py`` 组装，模块级不再有可变全局。"""

    def __init__(self, arms: Iterable[Arm]) -> None:
        entries = list(arms)
        names = [entry.name for entry in entries]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"arm 名重复: {', '.join(duplicates)}")
        self._by_name = {entry.name: entry for entry in entries}

    def names(self) -> tuple[str, ...]:
        return tuple(self._by_name)

    def arms(self) -> tuple[Arm, ...]:
        return tuple(self._by_name.values())

    def get(self, name: str) -> Arm:
        try:
            return self._by_name[name]
        except KeyError:
            raise UnknownArmError(name) from None

    def resolve(self, name: str, *, llm: Any) -> AgentFactory:
        return self.get(name).factory(llm=llm)


__all__ = [
    "AgentFactory",
    "Arm",
    "ArmRegistry",
    "UnknownArmError",
]
