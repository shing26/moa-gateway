"""任务运行器：物化工作区 → 跑 arm → 还原测试文件 → 验证 → 记 RunResult。

本包刻意**不做** re-export：``bench.agent.tools`` 要用 ``bench.harness.verify``
（bench 里唯一的子进程入口），而 ``bench.harness.runner`` 又要用
``bench.agent.tools`` 造工具集。如果在 ``__init__`` 里提前 import runner，就会
变成 ``agent.tools → harness.__init__ → runner → agent.tools`` 的循环。按子模块
直接 import 是这里唯一不绕的做法。
"""
