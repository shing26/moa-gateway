"""工作区工具：读文件、跑固定验证命令，以及它们与工作区的边界。

为什么还要一层策略检查
----------------------
``app/agents/tool_contract.py`` 已经把"注入 → 校验 → 分发"收敛成一处，但它判的是
**JSON Schema 能表达的东西**（类型、必填、未声明参数）。"这个路径是否落在工作区
内"不是 JSON Schema 能表达的——所以本模块在调用既有契约**之前**先过一次策略检查，
把越界判成 ``rejected``（模型填错参数，它能自己改），而不是 ``failed``（环境坏了）。
两种结果分开记账是既有的口径（ADR-018 决策 3），这里沿用，不改。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.agents.command_runner import run_command
from app.agents.tool_contract import ToolCallOutcome, call_tool
from app.agents.tools import AgentTool, ToolRegistry

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


def _as_int(value: object, default: int) -> int:
    """把模型给的整数参数转成 ``int``。

    真实模型会把 ``offset`` 写成数字字符串（``"160"``）。声明里允许 string 只是为了让
    这次调用能落地，这里仍按整数语义解释；解不出来就回默认值，绝不让它变成 500。
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


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
        if ".." in token.replace("\\", "/").split("/"):
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


async def call_workspace_tool(
    tool: AgentTool, arguments: dict | None, *, workspace: Path
) -> ToolCallOutcome:
    """策略检查 + 既有契约。两条路都要过，缺一不可。"""
    args = dict(arguments or {})
    rejection = check_tool_policy(tool.name, args, workspace=workspace)
    if rejection is not None:
        return ToolCallOutcome("rejected", rejection)
    return await call_tool(tool, args, session_id="")


#: 兼容旧名。bench 侧仍按 ``call_bench_tool`` 导入。
call_bench_tool = call_workspace_tool


def build_toolset(
    *,
    workspace: Path,
    verify_command: Sequence[str],
    limits: ToolLimits | None = None,
    names: Sequence[str] | None = None,
) -> ToolRegistry:
    """造一个**绑定到该工作区**的工具集。

    绑定发生在构造期而不是参数里：``run_tests`` 的基底命令来自任务定义，模型
    没有机会改它。
    """
    limits = limits or ToolLimits()
    root = workspace.resolve()
    def bind_workspace(target: Path) -> ToolRegistry:
        return build_toolset(
            workspace=target,
            verify_command=verify_command,
            limits=limits,
            names=names,
        )

    registry = ToolRegistry(bind_workspace)
    allowed = set(names) if names is not None else None

    def register(tool: AgentTool) -> None:
        if allowed is None or tool.name in allowed:
            registry.register(tool)

    async def read_file(path: str, offset: int = 1, limit: int = 0) -> str:
        """按行读取；``offset`` 1-based，``limit`` 是行数（0 = 到文件尾）。

        仍然受 ``read_bytes`` 约束——分页不是绕过观察上限的口子。真实模型会明确要求
        ``offset/limit``；不支持它时实测会反复重读整文件、把预算烧在无效调用上。
        """
        target = resolve_in_workspace(root, path)
        if not target.is_file():
            raise FileNotFoundError(f"不是文件: {path}")
        start = max(1, _as_int(offset, 1))
        want = max(0, _as_int(limit, 0))
        picked: list[tuple[int, str]] = []
        used = 0
        has_more = False
        last = start - 1
        with target.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            for lineno, line in enumerate(handle, start=1):
                if lineno < start:
                    continue
                if want and len(picked) >= want:
                    # 能走到这里说明后面还有一行，否则循环已经结束。
                    has_more = True
                    break
                encoded = len(line.encode("utf-8", "replace"))
                if used + encoded > limits.read_bytes:
                    if not picked:
                        # 单行就超上限：给截断后的这一行，避免 offset 原地打转。
                        picked.append((lineno, line.rstrip("\r\n")[:4000]))
                        last = lineno
                    has_more = True
                    break
                picked.append((lineno, line.rstrip("\r\n")))
                last = lineno
                used += encoded
        numbered = "\n".join(f"{i:>4}  {text}" for i, text in picked)
        if not numbered:
            return f"(第 {start} 行起没有内容)" if start > 1 else "(空文件)"
        if has_more:
            numbered += (
                f"\n…（已截断：返回 {len(picked)} 行；继续读请用 offset={last + 1}）"
            )
        return numbered

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

    register(
        AgentTool(
            name="read_file",
            description=(
                "按行读取工作区内的一个文本文件，返回带行号的原文。"
                "大文件用 offset/limit 分页，别反复重读整文件。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的文件路径"},
                    "offset": {
                        "type": ["integer", "string"],
                        "description": "从第几行开始读（1-based，默认 1）",
                    },
                    "limit": {
                        "type": ["integer", "string"],
                        "description": "最多读多少行（默认 0 = 到文件尾，仍受字节上限约束）",
                    },
                },
                "required": ["path"],
            },
            handler=read_file,
        )
    )
    register(
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
    register(
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
    register(
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
    register(
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
    "call_workspace_tool",
    "check_tool_policy",
    "parse_selector",
    "resolve_in_workspace",
]
