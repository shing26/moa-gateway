"""知识访问的中立 port（M4）：领域层不再反向依赖组合根 ``app.deps``。

此前 ``agents/tools.py`` 与 ``agent_core/tools_extra.py`` 在工具 handler
里 ``from app.deps import _retriever / knowledge_base``——领域层 import
组合根是分层坏味道（补强评估 M4）。本模块提供唯一的取用点：组合根在
装配时 ``configure()`` 注入实现，工具 handler 只 import 本模块。

测试注入方式：handler 内是延迟 ``from app.knowledge_access import
get_retriever``，因此 ``monkeypatch.setattr("app.knowledge_access.
get_retriever", lambda: fake)`` 即可替换，无需触碰组合根。

升级路径：现有消费方全部是 duck-typed；若需要静态约束，把
``KnowledgeBase``/``ContextRetriever`` 的接口提升为 ``typing.Protocol``
即可，本模块与消费方都不用改。
"""

from __future__ import annotations

from typing import Any

_STATE: dict[str, Any] = {"knowledge_base": None, "retriever": None}


def set_knowledge_base(knowledge_base: Any) -> None:
    _STATE["knowledge_base"] = knowledge_base


def set_retriever(retriever: Any) -> None:
    _STATE["retriever"] = retriever


def configure(
    *,
    knowledge_base: Any = None,
    retriever: Any = None,
) -> None:
    """组合根（app.deps）的一次性装配入口。"""
    if knowledge_base is not None:
        set_knowledge_base(knowledge_base)
    if retriever is not None:
        set_retriever(retriever)


def get_knowledge_base() -> Any:
    provider = _STATE.get("knowledge_base")
    if provider is None:
        raise RuntimeError(
            "knowledge_access 未配置：组合根应先调用 configure(knowledge_base=...)"
        )
    return provider


def get_retriever() -> Any:
    provider = _STATE.get("retriever")
    if provider is None:
        raise RuntimeError(
            "knowledge_access 未配置：组合根应先调用 configure(retriever=...)"
        )
    return provider


__all__ = [
    "configure",
    "get_knowledge_base",
    "get_retriever",
    "set_knowledge_base",
    "set_retriever",
]
