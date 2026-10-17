"""四档编排（§5）。``ARMS`` 是"哪些 arm 可用"的单一来源：CLI 与测试都读这里。

加一档 = 在下面 import 它的 ``Arm`` 并放进 ``ARMS``。别处不要各自维护一份可用清单
（argparse 的 choices、文档、测试里的字面量）——按名字查 ``ARMS``，默认档位读
``DEFAULT_ARM``。
"""

from __future__ import annotations

from bench.arms.l0 import L0
from bench.arms.l1 import L1
from bench.arms.l2 import L2
from bench.arms.l3 import L3
from bench.arms.registry import AgentFactory, Arm, ArmRegistry, UnknownArmError

ARMS = ArmRegistry([L0, L1, L2, L3])

#: 手跑 CLI 的缺省档位。零点：单体循环。
DEFAULT_ARM = L0.name

get_arm = ARMS.get
arm_names = ARMS.names
available_arms = ARMS.arms
resolve_arm = ARMS.resolve

__all__ = [
    "ARMS",
    "DEFAULT_ARM",
    "AgentFactory",
    "Arm",
    "ArmRegistry",
    "L0",
    "L1",
    "L2",
    "L3",
    "UnknownArmError",
    "arm_names",
    "available_arms",
    "get_arm",
    "resolve_arm",
]
