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
import shutil
import re
# Fixed git argv; never shell-expanded.
import subprocess  # nosec B404
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.agents.command_runner import resolve_program, run_command
from app.agents.subprocess_env import harden_git_argv, isolated_env
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

_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "node_modules",
    }
)
_BINARY_SNIFF = b"\x00"

#: ``replace_in_file`` 要把整个文件读进内存再写回。上限只为挡住误把
#: 巨型文件交给它的情况；超过时模型应改用分页读取 + 更小的编辑。
_MAX_EDIT_BYTES = 1_000_000

#: ``write_file`` 对**既有文档**的整体覆盖上限（行）。超过就必须用
#: ``replace_in_file`` 定点改。
#:
#: 这条线来自一次真实交付（2026-11-05，issue #43）：模型要给 489 行的 README
#: 加一句话，却用 ``write_file`` 重写了整份文件——验证脚本只断言了新增的
#: ``run_docker``，于是链接被截断（``logoColor`` 丢失）、表格里的相对链接变成
#: 纯文本、还凭空改掉一句事实（``Branch not protected`` → ``Branch not
#: supported``）。整文件重写等于让模型**转述**原文；转述不是编辑。
#:
#: 为什么只限文档：代码有测试门禁，转述坏了会被执行打回来；文档没有可执行的
#: 判据，"读起来还是那个意思"就是唯一的判据——于是写入方式本身必须成为门禁。
#: 代码文件保持可整份重写（bench 的 scripted expert 正是这么工作的）。
_MAX_DOC_OVERWRITE_LINES = 120
_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})

#: 模型把 ``replace_in_file`` 叫成别的名字时的兼容映射。真实轨迹里出现过
#: ``edit_file``（然后转去 ``write_file`` 整文件重写）。别名只在解析时生效，
#: 不进 schema：多宣告一个名字就多一份提示预算。
_TOOL_ALIASES = {"edit_file": "replace_in_file"}


def _read_for_edit(path: Path) -> tuple[str, bool]:
    """读取待编辑文本，返回 ``(LF 规范化文本, 原文件是否使用 CRLF)``。

    模型看到的 ``read_file`` 输出已经去掉了 ``\\r``，它给出的 ``old`` 自然是
    LF 风格；如果文件本身是 CRLF，直接 ``str.count`` 会永远匹配不到。这里
    统一成 LF 做匹配，写回时再按原文件的换行恢复。
    """
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        raw = handle.read()
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def _clip_observation(text: str, limit: int) -> str:
    """保留头尾两段。diff 的关键信息可能在前几个 hunk，也可能在末尾。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n…（中略 {len(text) - limit} 字符）…\n{text[-tail:]}"


def _run_git(args: list[str], *, cwd: Path, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run a read-only git command with the hardened argv and allowlisted env."""
    env = isolated_env(cwd, git=True)
    argv = resolve_program(harden_git_argv(args), env)
    return subprocess.run(  # noqa: S603  # nosec B603 - argv is built locally, shell=False
        argv,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        shell=False,
        check=False,
    )


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
    relative = candidate.relative_to(root_resolved)
    if any(part.lower() == ".git" for part in relative.parts):
        raise PathEscape(f"路径指向 Git 元数据: {raw!r}")
    return candidate


def _resolved_for_write(root: Path, raw: str) -> Path:
    """Resolve a write target without following an existing final symlink."""
    root_resolved = root.resolve()
    lexical = (root_resolved / (raw or ".")).absolute()
    try:
        relative = lexical.relative_to(root_resolved)
    except ValueError as exc:
        raise PathEscape(f"路径越出工作区: {raw!r}") from exc
    if any(part.lower() == ".git" for part in relative.parts):
        raise PathEscape(f"路径指向 Git 元数据: {raw!r}")

    parent = lexical.parent.resolve()
    if parent != root_resolved and not parent.is_relative_to(root_resolved):
        raise PathEscape(f"路径越出工作区: {raw!r}")
    resolved = parent / lexical.name
    if resolved.is_symlink():
        raise PathEscape(f"路径是符号链接: {raw!r}")
    if any(part.lower() == ".git" for part in resolved.relative_to(root_resolved).parts):
        raise PathEscape(f"路径指向 Git 元数据: {raw!r}")
    return resolved


def create_verification_workspace(root: Path) -> tuple[Path, Path]:
    """Copy the workspace without ``.git`` or local caches for verification."""
    parent = Path(tempfile.mkdtemp(prefix="moa-verify-"))
    target = parent / "workspace"
    source = Path(root).resolve()

    def copy_file(source_path: str, target_path: str) -> None:
        item = Path(source_path)
        if item.is_symlink() or not item.is_file():
            raise OSError(f"验证副本只允许普通文件: {source_path}")
        destination = Path(target_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        current = Path(directory)
        for name in names:
            candidate = current / name
            if name in _SKIP_DIRS:
                ignored.add(name)
                continue
            if candidate.is_symlink():
                raise OSError(f"工作区内不允许符号链接: {candidate}")
        return ignored

    try:
        shutil.copytree(
            source,
            target,
            ignore=ignore,
            copy_function=copy_file,
        )
    except Exception:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    return target, parent


def _split_selector_tokens(text: str) -> list[str]:
    """按空白切分，但引号内的空白留在一个 token 里。

    ``-k "needs_human or never_reopens"`` 必须变成 ``-k`` 与
    ``needs_human or never_reopens`` 两个 token。``str.split()`` 会切成
    ``-k`` / ``"needs_human`` / ``or`` / ``never_reopens"``，pytest 于是把
    ``or`` 当成路径，回一句 "file or directory not found: or"——报错说不到
    点子上，模型只好反复重试同一条命令。

    不用 ``shlex``：POSIX 模式会把 Windows 路径里的反斜杠当转义吃掉。
    """
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if quote:
        raise ValueError("unclosed quote")
    if current:
        tokens.append("".join(current))
    return tokens


def parse_selector(selector: str) -> tuple[list[str], str | None]:
    """把 ``selector`` 拆成可安全追加的参数。返回 ``(tokens, 拒绝原因)``。

    基底命令不可变——这是"不允许换可执行文件"的结构性保证：tokens 只会被
    **追加**在任务定义好的 argv 后面。引号只用于把带空白的表达式（典型是
    ``-k "a or b"``）合成一个参数，引号本身不进 argv。
    """
    text = (selector or "").strip()
    if not text:
        return [], None
    try:
        raw_tokens = _split_selector_tokens(text)
    except ValueError:
        return [], "筛选参数里的引号未闭合"
    tokens: list[str] = []
    for token in raw_tokens:
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


def _indentation_hint(text: str, needle: str) -> str:
    """把"没匹配到"变成一句能照着改的话。

    只说"空格、缩进必须一致"没有用：模型看不见自己抄错了几格。这里用 old 的
    第一行做锚点，把文件里的**真实**写法连引号一起交回去（``repr`` 保留前导
    空格），它下一次就能直接对上。
    """
    anchor = next((line for line in needle.splitlines() if line.strip()), "")
    if not anchor:
        return "old 不能为空字符串"
    wanted = anchor.strip()
    for line in text.splitlines():
        if line.strip() == wanted:
            return (
                f"old 的第一行在文件中的实际写法是 {line!r}"
                "（引号内保留了真实前导空格）；行号前缀不属于文件内容"
            )
    return (
        f"old 的第一行 {wanted!r} 在文件中不存在；"
        "请先用 read_file 确认原文，行号前缀不属于文件内容"
    )


def _check_replacement(target: Path, display: str, arguments: dict) -> str | None:
    """Reject inexact/ambiguous replacements before they become a failed call.

    A wrong ``old`` snippet is a model-input mistake, not an environment
    failure: marking it ``rejected`` lets the loop feed the reason back and
    retry without spending a "tool failed" budget.
    """
    old = arguments.get("old")
    if not isinstance(old, str) or old == "":
        return "old 不能为空字符串"
    if not isinstance(arguments.get("new"), str):
        return "new 必须是字符串"
    if not target.exists():
        return f"文件不存在: {display}；replace_in_file 只修改已存在的文件"
    if not target.is_file():
        return f"不是文件: {display}"
    if target.stat().st_size > _MAX_EDIT_BYTES:
        return f"文件超过 {_MAX_EDIT_BYTES} 字节，replace_in_file 不适用: {display}"

    text, _ = _read_for_edit(target)
    needle = old.replace("\r\n", "\n")
    count = text.count(needle)
    if count == 0:
        return "未找到 old 的精确匹配；" + _indentation_hint(text, needle)
    if count > 1 and arguments.get("replace_all") is not True:
        return (
            f"old 匹配到 {count} 处；请提供更长的唯一片段，"
            "或设置 replace_all=true"
        )
    return None


_MAX_SUMMARY_BYTES = 2_000_000


def _existing_lines(target: Path) -> list[str] | None:
    """覆盖前把原文件读成行；新文件、读不了或过大时返回 ``None``。"""
    try:
        if not target.is_file() or target.stat().st_size > _MAX_SUMMARY_BYTES:
            return None
        with target.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read().replace("\r\n", "\n").splitlines()
    except OSError:
        return None


def _overwrite_summary(before: list[str] | None, body: str) -> str:
    """整文件覆盖时，把"删掉了多少行"直接说给模型听。

    真实轨迹（2026-11-05，第 5 行交付）：模型用 ``write_file`` 重写 README，把
    没打算改的链接、徽章参数与事实一起改掉；验证脚本只断言了新增的那几句，于是
    一路绿灯走到人审。差别只有 37 行——占全文约 3%，任何相似度阈值都抓不住。
    所以这里不判对错，只把事实摊开：覆盖前多少行、覆盖后多少行、多少行真的
    消失了。按行多重集比较，不做重排敏感的 diff（大文件上 SequenceMatcher 是
    平方复杂度）。
    """
    if before is None:
        return ""
    after = body.splitlines()
    if before == after:
        return "；整文件覆盖：内容未变"
    old = Counter(before)
    new = Counter(after)
    removed = sum((old - new).values())
    added = sum((new - old).values())
    hint = "；只想改其中几行时用 replace_in_file" if removed else ""
    return (
        f"；整文件覆盖：原 {len(before)} 行 → 新 {len(after)} 行，"
        f"删除 {removed} 行、新增 {added} 行{hint}"
    )


def _check_overwrite(target: Path, display: str) -> str | None:
    """拒绝用 ``write_file`` 整体覆盖大文档。

    判成 ``rejected``（模型能自己改）而不是 ``failed``：换一个更窄的编辑动作
    就能继续，这正是"工具用错了"，不是环境坏了。
    """
    if target.suffix.lower() not in _DOC_SUFFIXES:
        return None
    if not target.exists() or not target.is_file():
        return None
    try:
        with target.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            lines = sum(1 for _ in handle)
    except OSError:
        return None
    if lines <= _MAX_DOC_OVERWRITE_LINES:
        return None
    return (
        f"{display} 已有 {lines} 行，超过 write_file 整体覆盖文档的上限 "
        f"{_MAX_DOC_OVERWRITE_LINES} 行。请改用 replace_in_file 做定点修改："
        "整文件重写会让模型转述原文，把没打算改的链接、措辞与事实一起改掉。"
    )


def check_tool_policy(name: str, arguments: dict, *, workspace: Path) -> str | None:
    """策略前置检查。返回拒绝原因，或 ``None`` 表示放行给既有契约。"""
    raw = str(arguments.get("path", ".") or ".")
    if name in ("read_file", "list_files", "search", "git_diff"):
        try:
            target = resolve_in_workspace(workspace, raw)
        except PathEscape as exc:
            return str(exc)
        if name == "git_diff":
            relative = target.relative_to(workspace.resolve()).as_posix()
            if relative.startswith(("-", ":")):
                return f"path 不能以 - 或 : 开头: {raw!r}"
        return None
    if name in ("write_file", "replace_in_file"):
        if not raw.strip():
            return "path 不能为空"
        try:
            target = _resolved_for_write(workspace, raw)
        except PathEscape as exc:
            return str(exc)
        if name == "replace_in_file":
            return _check_replacement(target, raw, arguments)
        return _check_overwrite(target, raw)
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

    registry = ToolRegistry(bind_workspace, aliases=_TOOL_ALIASES)
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
        # 行号与正文之间用 ``|`` 分隔，而不是两个空格：旧格式 ``   76  - name:``
        # 让模型把"行号 + 两个空格"当成缩进读走——真实轨迹里它因此给 6 空格缩进
        # 的行写了 8 个空格，随后 12 次 replace_in_file 全部"未找到精确匹配"，
        # 步数预算耗尽（2026-11-05，issue #43 第 2 次尝试）。分隔符要能一眼看见。
        numbered = "\n".join(f"{i:>4}|{text}" for i, text in picked)
        if not numbered:
            return f"(第 {start} 行起没有内容)" if start > 1 else "(空文件)"
        if has_more:
            numbered += (
                f"\n…（已截断：返回 {len(picked)} 行；继续读请用 offset={last + 1}）"
            )
        return numbered

    async def write_file(path: str, content: str) -> str:
        target = _resolved_for_write(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = content if content.endswith("\n") else content + "\n"
        before = _existing_lines(target)
        # newline="" 是必须的：text 模式默认把 \n 翻译成 os.linesep，在 Windows 上
        # 会把每行都变成 CRLF，于是"改了一个字符"在 git diff 里显示成整个文件重写
        # ——patch 是判决的证据，不能被换行翻译污染。
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        return f"已写入 {path}（{len(content)} 字符）{_overwrite_summary(before, body)}"

    async def replace_in_file(
        path: str, old: str, new: str, replace_all: bool = False
    ) -> str:
        """Exact-match replacement; the policy check already vetted ambiguity."""
        target = _resolved_for_write(root, path)
        if not target.is_file():
            raise FileNotFoundError(f"不是文件: {path}")
        if target.stat().st_size > _MAX_EDIT_BYTES:
            raise ValueError(f"文件超过 {_MAX_EDIT_BYTES} 字节: {path}")
        text, uses_crlf = _read_for_edit(target)
        needle = old.replace("\r\n", "\n")
        replacement = new.replace("\r\n", "\n")
        count = text.count(needle)
        if count == 0:
            raise ValueError("未找到 old 的精确匹配")
        if count > 1 and not replace_all:
            raise ValueError(f"old 匹配到 {count} 处，需要 replace_all=true")
        updated = text.replace(needle, replacement) if replace_all else text.replace(
            needle, replacement, 1
        )
        # 源文件以换行结尾时保持这个约定；不替模型决定是否去掉文件末尾换行。
        if text.endswith("\n") and not updated.endswith("\n"):
            updated += "\n"
        body = updated.replace("\n", "\r\n") if uses_crlf else updated
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(body)
        replaced = count if replace_all else 1
        return f"已替换 {path} 中的 {replaced} 处"

    async def git_diff(path: str = ".", stat_only: bool = False) -> str:
        """Read-only self-review against the workspace's baseline commit."""
        target = resolve_in_workspace(root, path)
        relative = target.relative_to(root).as_posix()
        if relative.startswith(("-", ":")):
            raise ValueError("path 不能以 - 或 : 开头")
        if not (root / ".git").exists():
            raise RuntimeError("工作区缺少 .git，无法与 baseline 比较")

        try:
            status = await asyncio.to_thread(
                _run_git,
                ["status", "--porcelain", "--", relative],
                cwd=root,
                timeout_s=limits.command_timeout_s,
            )
            diff_args = [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--stat",
            ]
            if not stat_only:
                diff_args.extend(["--patch", "--unified=3"])
            diff_args.extend(["--", relative])
            diff = await asyncio.to_thread(
                _run_git,
                diff_args,
                cwd=root,
                timeout_s=limits.command_timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"git 命令超过 {limits.command_timeout_s:.0f}s 未完成"
            ) from exc

        for result, label in ((status, "status"), (diff, "diff")):
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()[:2000]
                raise RuntimeError(f"git {label} 失败（exit={result.returncode}）: {detail}")

        sections: list[str] = []
        status_text = (status.stdout or "").strip()
        if status_text:
            sections.append(f"$ git status --porcelain -- {relative}\n{status_text}")
        diff_text = (diff.stdout or "").rstrip()
        if diff_text:
            sections.append(f"$ git diff -- {relative}\n{diff_text}")
        elif status_text:
            sections.append(
                "（status 中的未跟踪文件不会出现在 git diff 里；用 read_file 查看内容）"
            )
        if not sections:
            if not target.exists():
                return f"路径不存在，且相对 baseline 没有改动: {path}"
            return "工作区相对 baseline 没有改动。"
        return _clip_observation("\n\n".join(sections), limits.output_chars)

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
        # ``rglob`` on a *file* yields nothing, so passing a file path used to
        # answer "扫描 0 个文件" — a lie that costs the model a step every time
        # it narrows the search to one file it just read. Search the file.
        candidates = [target] if target.is_file() else sorted(target.rglob("*"))
        for item in candidates:
            if len(hits) >= limits.search_matches or scanned >= limits.search_files:
                break
            if not item.is_file():
                continue
            try:
                scanned_path = resolve_in_workspace(root, str(item))
            except PathEscape:
                continue
            rel_parts = scanned_path.relative_to(root).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            if scanned_path.stat().st_size > limits.search_file_bytes:
                continue
            raw = scanned_path.read_bytes()
            if _BINARY_SNIFF in raw:
                continue
            scanned += 1
            rel = scanned_path.relative_to(root).as_posix()
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
        verification_root, temporary_parent = await asyncio.to_thread(
            create_verification_workspace, root
        )
        try:
            result = await asyncio.to_thread(
                run_command,
                argv,
                cwd=verification_root,
                timeout_s=limits.command_timeout_s,
                output_limit=limits.output_chars,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, temporary_parent, True)
        status = "通过" if result.passed else "未通过"
        return (
            f"$ {' '.join(argv)}\n退出码 {result.exit_code}（{status}）\n{result.output}"
        )

    register(
        AgentTool(
            name="read_file",
            description=(
                "按行读取工作区内的一个文本文件，返回 `行号|原文`。"
                "行号前缀（含竖线）不属于文件内容，不要抄进 replace_in_file 的 old。"
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
            description=(
                "把内容整体写入工作区内的一个文件，父目录会自动创建。"
                f"既有的超过 {_MAX_DOC_OVERWRITE_LINES} 行的文档（.md/.rst/.txt）"
                "不允许整体覆盖——改文档请用 replace_in_file。"
            ),
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
            name="replace_in_file",
            description=(
                "精确替换已存在文件中的一段文本。old 必须与原文逐字符一致（含空格、"
                "缩进、换行）；默认要求只匹配到一处。小改动优先用它，避免整文件重写。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的文件路径"},
                    "old": {
                        "type": "string",
                        "description": "要替换的原文片段；必须唯一匹配（除非 replace_all=true）",
                    },
                    "new": {"type": "string", "description": "替换后的新文本"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "为 true 时替换所有匹配；默认 false（要求唯一匹配）",
                    },
                },
                "required": ["path", "old", "new"],
            },
            handler=replace_in_file,
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
            name="git_diff",
            description=(
                "只读查看工作区相对 baseline 提交的改动：先列 git status，再给 diff。"
                "完成修改后用它自检；未跟踪文件只在 status 中出现。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "限定文件或目录，默认 .（整个工作区）",
                    },
                    "stat_only": {
                        "type": "boolean",
                        "description": "为 true 时只返回改动统计，不返回 patch",
                    },
                },
            },
            handler=git_diff,
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
    "create_verification_workspace",
    "parse_selector",
    "resolve_in_workspace",
]
