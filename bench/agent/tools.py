"""§5.0 的五个工具，以及它们与工作区的边界。

为什么还要一层策略检查
----------------------
``app/agents/tool_contract.py`` 已经把"注入 → 校验 → 分发"收敛成一处，但它判的是
**JSON Schema 能表达的东西**（类型、必填、未声明参数）。"这个路径是否落在工作区
内"不是 JSON Schema 能表达的——所以本模块在调用既有契约**之前**先过一次策略检查，
把越界判成 ``rejected``（模型填错参数，它能自己改），而不是 ``failed``（环境坏了）。
两种结果分开记账是既有的口径（ADR-018 决策 3），这里沿用，不改。

``app/`` 零改动：``AgentTool`` / ``ToolRegistry`` / ``call_tool`` 全是原样复用，
工具集是 bench 自己 new 出来的一个 ``ToolRegistry`` 实例，不污染全局注册表。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.agents.tool_contract import ToolCallOutcome, call_tool
from app.agents.tools import AgentTool, ToolRegistry

from bench.harness.verify import run_command

#: 允许 agent 追加到验证命令后面的筛选开关。**白名单**，不是黑名单：
#: 允许列表之外的一切（含 ``--rootdir`` / ``--confcutdir`` / ``-p``）一律被拒，
#: 免得筛选参数变成"换个方式改判据"。
_ALLOWED_SELECTOR_FLAGS = frozenset(
    {
        "-k",
        "-x",
        "-q",
        "-v",
        "-s",
        "-m",
        "--maxfail",
        "--tb",
        "--durations",
        "--lf",
        "--ff",
        "--last-failed",
        "--failed-first",
        "--no-header",
    }
)

_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", ".pytest_cache", "node_modules"})
_BINARY_SNIFF = b"\x00"


@dataclass(frozen=True)
class ToolLimits:
    """工具的观察上限。存在的理由：观察直接变成 token，而成本是本次的主判据。"""

    read_bytes: int = 40_000
    list_entries: int = 400
    search_matches: int = 50
    search_files: int = 2_000
    search_file_bytes: int = 1_000_000
    output_chars: int = 20_000
    command_timeout_s: float = 300.0


class PathEscape(Exception):
    """路径解析后落在工作区之外。"""


def resolve_in_workspace(root: Path, raw: str) -> Path:
    """把 ``raw`` 解析成工作区内的绝对路径，否则抛 ``PathEscape``。

    ``resolve()`` 会跟随符号链接，所以"工作区里放一个指向 /etc 的软链"这条经典
    绕过会在这里被拦下——判定依据是**解析后**的位置，不是字符串前缀。
    """
    root_resolved = root.resolve()
    candidate = (root_resolved / (raw or ".")).resolve()
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        raise PathEscape(f"路径越出工作区: {raw!r}")
    return candidate


def parse_selector(selector: str) -> tuple[list[str], str | None]:
    """把 ``selector`` 拆成可安全追加的参数。返回 ``(tokens, 拒绝原因)``。

    基底命令不可变——这是"不允许换可执行文件"的结构性保证：tokens 只会被
    **追加**在任务定义好的 argv 后面。
    """
    text = (selector or "").strip()
    if not text:
        return [], None
    tokens: list[str] = []
    for token in text.split():
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            if flag not in _ALLOWED_SELECTOR_FLAGS:
                return [], f"筛选参数 {flag} 不在允许列表内"
            tokens.append(token)
            continue
        # 位置参数：只允许工作区内的相对路径或 pytest nodeid。
        if token.startswith("/") or token.startswith("\\") or re.match(r"^[A-Za-z]:", token):
            return [], f"筛选参数 {token} 不是工作区内的相对路径"
        if ("/" in token or "\\" in token) and ".." in token.replace("\\", "/").split("/"):
            return [], f"筛选参数 {token} 含上级目录"
        tokens.append(token)
    return tokens, None


def check_tool_policy(name: str, arguments: dict, *, workspace: Path) -> str | None:
    """策略前置检查。返回拒绝原因，或 ``None`` 表示放行给既有契约。"""
    if name in ("read_file", "write_file", "list_files", "search"):
        try:
            resolve_in_workspace(workspace, str(arguments.get("path", ".") or "."))
        except PathEscape as exc:
            return str(exc)
        return None
    if name == "run_tests":
        _, reason = parse_selector(str(arguments.get("selector", "") or ""))
        return reason
    return None


async def call_bench_tool(
    tool: AgentTool, arguments: dict | None, *, workspace: Path
) -> ToolCallOutcome:
    """策略检查 + 既有契约。两条路都要过，缺一不可。"""
    args = dict(arguments or {})
    rejection = check_tool_policy(tool.name, args, workspace=workspace)
    if rejection is not None:
        return ToolCallOutcome("rejected", rejection)
    return await call_tool(tool, args, session_id="")


def build_toolset(
    *,
    workspace: Path,
    verify_command: Sequence[str],
    limits: ToolLimits | None = None,
) -> ToolRegistry:
    """造一个**绑定到该工作区**的工具集。

    绑定发生在构造期而不是参数里：``run_tests`` 的基底命令来自任务定义，模型
    没有机会改它。
    """
    limits = limits or ToolLimits()
    root = workspace.resolve()
    registry = ToolRegistry()

    async def read_file(path: str) -> str:
        target = resolve_in_workspace(root, path)
        if not target.is_file():
            raise FileNotFoundError(f"不是文件: {path}")
        raw = target.read_bytes()
        truncated = len(raw) > limits.read_bytes
        body = raw[: limits.read_bytes].decode("utf-8", "replace")
        numbered = "\n".join(
            f"{i:>4}  {line}" for i, line in enumerate(body.splitlines(), start=1)
        )
        if truncated:
            numbered += f"\n…（文件超过 {limits.read_bytes} 字节，已截断）"
        return numbered or "(空文件)"

    async def write_file(path: str, content: str) -> str:
        target = resolve_in_workspace(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = content if content.endswith("\n") else content + "\n"
        # newline="" 是必须的：text 模式默认把 \n 翻译成 os.linesep，在 Windows 上
        # 会把每行都变成 CRLF，于是"改了一个字符"在 git diff 里显示成整个文件重写
        # ——patch 是判决的证据，不能被换行翻译污染。
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        return f"已写入 {path}（{len(content)} 字符）"

    async def list_files(path: str = ".") -> str:
        target = resolve_in_workspace(root, path)
        if not target.exists():
            raise FileNotFoundError(f"路径不存在: {path}")
        entries: list[str] = []
        for item in sorted(target.rglob("*")):
            if any(part in _SKIP_DIRS for part in item.relative_to(root).parts):
                continue
            rel = item.relative_to(root).as_posix()
            entries.append(f"{rel}/" if item.is_dir() else rel)
            if len(entries) >= limits.list_entries:
                entries.append(f"…（超过 {limits.list_entries} 项，已截断）")
                break
        return "\n".join(entries) or "(空目录)"

    async def search(pattern: str, path: str = ".") -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"正则不合法: {exc}") from exc
        target = resolve_in_workspace(root, path)
        hits: list[str] = []
        scanned = 0
        for item in sorted(target.rglob("*")):
            if len(hits) >= limits.search_matches or scanned >= limits.search_files:
                break
            if not item.is_file():
                continue
            rel_parts = item.relative_to(root).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            if item.stat().st_size > limits.search_file_bytes:
                continue
            raw = item.read_bytes()
            if _BINARY_SNIFF in raw:
                continue
            scanned += 1
            rel = item.relative_to(root).as_posix()
            for lineno, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(hits) >= limits.search_matches:
                        break
        if not hits:
            return f"无匹配（扫描 {scanned} 个文件）"
        return "\n".join(hits)

    async def run_tests(selector: str = "") -> str:
        tokens, reason = parse_selector(selector)
        if reason is not None:
            raise ValueError(reason)
        argv = [*verify_command, *tokens]
        result = await asyncio.to_thread(
            run_command,
            argv,
            cwd=root,
            timeout_s=limits.command_timeout_s,
            output_limit=limits.output_chars,
        )
        status = "通过" if result.passed else "未通过"
        return (
            f"$ {' '.join(argv)}\n退出码 {result.exit_code}（{status}）\n{result.output}"
        )

    registry.register(
        AgentTool(
            name="read_file",
            description="读取工作区内的一个文本文件，返回带行号的原文。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的文件路径"}
                },
                "required": ["path"],
            },
            handler=read_file,
        )
    )
    registry.register(
        AgentTool(
            name="write_file",
            description="把内容整体写入工作区内的一个文件，父目录会自动创建。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的文件路径"},
                    "content": {"type": "string", "description": "文件的完整新内容"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        )
    )
    registry.register(
        AgentTool(
            name="list_files",
            description="列出工作区内某个目录下的全部文件与子目录（相对路径）。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的目录，默认 ."}
                },
            },
            handler=list_files,
        )
    )
    registry.register(
        AgentTool(
            name="search",
            description="在工作区内按正则搜索文本，返回 文件:行号: 内容。",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python 正则表达式"},
                    "path": {"type": "string", "description": "搜索起点目录，默认 ."},
                },
                "required": ["pattern"],
            },
            handler=search,
        )
    )
    registry.register(
        AgentTool(
            name="run_tests",
            description=(
                "运行本任务的验证命令并返回输出。基底命令由任务固定，"
                "只能通过 selector 追加筛选（如某个测试文件、::某测试、-k 表达式）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "追加的筛选参数，例如 tests/unit/test_x.py 或 -k login",
                    }
                },
            },
            handler=run_tests,
        )
    )
    return registry


__all__ = [
    "PathEscape",
    "ToolLimits",
    "build_toolset",
    "call_bench_tool",
    "check_tool_policy",
    "parse_selector",
    "resolve_in_workspace",
]
