"""分层结构断言（M4 的回归门禁）。

补强评估 M4 指出 agents/agent_core 反向 import 组合根（app.deps）是
分层坏味道；已改为经 ``app.knowledge_access`` 中立 port 取用。本测试
把"领域层不 import 组合根"钉进 CI，防止将来出现新的反向依赖。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 领域/能力层目录：允许 import 中立 port 与能力模块，不允许 import 组合根
DOMAIN_DIRS = ("app/agents", "app/agent_core")
FORBIDDEN_IMPORTS = ("from app.deps import", "import app.deps")


def test_domain_layers_do_not_import_composition_root() -> None:
    offenders: list[str] = []
    for rel_dir in DOMAIN_DIRS:
        for path in (REPO_ROOT / rel_dir).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if any(stripped.startswith(f) for f in FORBIDDEN_IMPORTS):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}")
    assert not offenders, f"领域层出现对组合根 app.deps 的反向依赖: {offenders}"


def test_knowledge_access_port_is_configured_by_composition_root() -> None:
    deps_text = (REPO_ROOT / "app" / "deps.py").read_text(encoding="utf-8")
    assert "_configure_knowledge_access(" in deps_text, (
        "组合根应调用 knowledge_access.configure() 注入实现"
    )
