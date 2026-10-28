"""§5.0 工具层：工作区边界、上限，以及"筛选参数不能变成换判据"。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from bench.agent.tools import (
    PathEscape,
    ToolLimits,
    build_toolset,
    call_bench_tool,
    parse_selector,
    resolve_in_workspace,
)

#: 假验证命令：把收到的参数原样打出来，好断言"selector 只是被追加"。
PRINT_ARGV = (sys.executable, "-c", "import sys; print('argv:', sys.argv[1:])")


def make_tools(workspace: Path, **limits: int):
    return build_toolset(
        workspace=workspace,
        verify_command=PRINT_ARGV,
        limits=ToolLimits(**limits) if limits else None,
    )


def test_resolve_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(PathEscape):
        resolve_in_workspace(tmp_path, "../../etc/passwd")


@pytest.mark.parametrize(
    "path",
    (
        ".git",
        ".git/config",
        ".GIT/config",
        "nested/.git/config",
    ),
)
def test_resolve_rejects_git_metadata(tmp_path: Path, path: str) -> None:
    with pytest.raises(PathEscape, match="Git 元数据"):
        resolve_in_workspace(tmp_path, path)


@pytest.mark.asyncio
async def test_read_outside_workspace_is_rejected_not_failed(tmp_path: Path) -> None:
    tools = make_tools(tmp_path)
    outcome = await call_bench_tool(
        tools.get("read_file"), {"path": "../outside.txt"}, workspace=tmp_path
    )
    # 越界是"模型填错参数"（它能自己改），不是"环境坏了"——口径沿用 ADR-018 决策 3。
    assert outcome.kind == "rejected"


@pytest.mark.asyncio
async def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    secret = tmp_path.parent / "bench-outside-secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    tools = make_tools(tmp_path)
    outcome = await call_bench_tool(
        tools.get("read_file"), {"path": "link.txt"}, workspace=tmp_path
    )
    # 判定依据是 resolve() 之后的位置，不是字符串前缀。
    assert outcome.kind == "rejected"


@pytest.mark.asyncio
async def test_search_does_not_follow_symlinks_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside needle\n", encoding="utf-8")
    link = workspace / "linked.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    outcome = await call_bench_tool(
        make_tools(workspace).get("search"),
        {"pattern": "outside needle"},
        workspace=workspace,
    )

    assert outcome.kind == "ok"
    assert "无匹配" in outcome.detail
    assert "outside needle" not in outcome.detail


@pytest.mark.asyncio
async def test_write_does_not_follow_a_symlink_final_component(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    link = workspace / "linked.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    outcome = await call_bench_tool(
        make_tools(workspace).get("write_file"),
        {"path": "linked.txt", "content": "overwritten"},
        workspace=workspace,
    )

    assert outcome.kind == "rejected"
    assert outside.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.asyncio
async def test_write_rejects_a_symlinked_parent_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "linked-dir"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    outcome = await call_bench_tool(
        make_tools(workspace).get("write_file"),
        {"path": "linked-dir/escaped.txt", "content": "nope"},
        workspace=workspace,
    )

    assert outcome.kind == "rejected"
    assert not (outside / "escaped.txt").exists()


@pytest.mark.asyncio
async def test_run_tests_cannot_read_git_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "secret").write_text("git-secret\n", encoding="utf-8")
    (workspace / "probe.py").write_text(
        "from pathlib import Path\n"
        "secret = Path('.git/secret')\n"
        "raise SystemExit(0 if secret.exists() else 3)\n",
        encoding="utf-8",
    )
    from app.agents.workspace_tools import build_toolset

    tools = build_toolset(
        workspace=workspace,
        verify_command=(sys.executable, "probe.py"),
        names=("run_tests",),
    )
    outcome = await call_bench_tool(
        tools.get("run_tests"),
        {},
        workspace=workspace,
    )

    assert outcome.kind == "ok"
    assert "退出码 3" in outcome.detail


@pytest.mark.asyncio
async def test_write_outside_workspace_is_rejected(tmp_path: Path) -> None:
    tools = make_tools(tmp_path)
    outcome = await call_bench_tool(
        tools.get("write_file"),
        {"path": "../escaped.txt", "content": "nope"},
        workspace=tmp_path,
    )
    assert outcome.kind == "rejected"
    assert not (tmp_path.parent / "escaped.txt").exists()


@pytest.mark.asyncio
async def test_workspace_toolset_can_be_rebound_for_an_isolated_copy(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    isolated = tmp_path / "isolated"
    original.mkdir()
    isolated.mkdir()
    tools = make_tools(original)

    rebound = tools.for_workspace(isolated)
    outcome = await call_bench_tool(
        rebound.get("write_file"),
        {"path": "isolated.txt", "content": "only here"},
        workspace=isolated,
    )

    assert outcome.kind == "ok"
    assert (isolated / "isolated.txt").read_text(encoding="utf-8") == "only here\n"
    assert not (original / "isolated.txt").exists()


@pytest.mark.asyncio
async def test_read_file_numbers_lines_and_truncates(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("abcdefghij\n", encoding="utf-8")
    tools = make_tools(tmp_path, read_bytes=5)
    outcome = await call_bench_tool(
        tools.get("read_file"), {"path": "big.txt"}, workspace=tmp_path
    )
    assert outcome.kind == "ok"
    assert "已截断" in outcome.detail


@pytest.mark.asyncio
async def test_read_file_paginates_by_offset_and_limit(tmp_path: Path) -> None:
    # 真实模型明确要求分页；不支持 offset/limit 时它会反复重读整文件，把预算烧光。
    (tmp_path / "f.txt").write_text(
        "line one\nline two\nline three\nline four\n", encoding="utf-8"
    )
    tools = make_tools(tmp_path)

    outcome = await call_bench_tool(
        tools.get("read_file"),
        {"path": "f.txt", "offset": 2, "limit": 2},
        workspace=tmp_path,
    )

    assert outcome.kind == "ok"
    assert "line two" in outcome.detail
    assert "line three" in outcome.detail
    assert "line one" not in outcome.detail
    assert "line four" not in outcome.detail
    # 后面还有内容，要给下一页的锚点。
    assert "offset=4" in outcome.detail


@pytest.mark.asyncio
async def test_read_file_accepts_numeric_strings_from_the_model(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    tools = make_tools(tmp_path)

    # 模型实测会把 offset/limit 写成字符串；契约允许 string，handler 按整数解释。
    outcome = await call_bench_tool(
        tools.get("read_file"),
        {"path": "f.txt", "offset": "2", "limit": "1"},
        workspace=tmp_path,
    )

    assert outcome.kind == "ok"
    assert "   2  b" in outcome.detail
    assert "   1  a" not in outcome.detail


@pytest.mark.asyncio
async def test_read_file_offset_past_eof_is_explicit(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a\nb\n", encoding="utf-8")
    tools = make_tools(tmp_path)

    outcome = await call_bench_tool(
        tools.get("read_file"), {"path": "f.txt", "offset": 99}, workspace=tmp_path
    )

    assert outcome.kind == "ok"
    assert "第 99 行起没有内容" in outcome.detail


@pytest.mark.asyncio
async def test_read_file_pagination_still_respects_the_byte_cap(tmp_path: Path) -> None:
    # 显式 LF：Windows 的 write_text 默认写 CRLF，会让这里的字节算术变成平台相关。
    (tmp_path / "f.txt").write_text(
        "aaaa\nbbbb\ncccc\n", encoding="utf-8", newline="\n"
    )
    tools = make_tools(tmp_path, read_bytes=11)

    outcome = await call_bench_tool(
        tools.get("read_file"), {"path": "f.txt"}, workspace=tmp_path
    )

    assert outcome.kind == "ok"
    assert "已截断" in outcome.detail
    assert "继续读请用 offset=3" in outcome.detail


@pytest.mark.asyncio
async def test_list_files_truncates_at_limit(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")
    tools = make_tools(tmp_path, list_entries=3)
    outcome = await call_bench_tool(
        tools.get("list_files"), {"path": "."}, workspace=tmp_path
    )
    assert outcome.kind == "ok"
    assert "已截断" in outcome.detail


@pytest.mark.asyncio
async def test_search_truncates_at_limit(tmp_path: Path) -> None:
    (tmp_path / "hits.txt").write_text(
        "\n".join(f"needle {index}" for index in range(10)), encoding="utf-8"
    )
    tools = make_tools(tmp_path, search_matches=2)
    outcome = await call_bench_tool(
        tools.get("search"), {"pattern": "needle"}, workspace=tmp_path
    )
    assert outcome.kind == "ok"
    assert len([line for line in outcome.detail.splitlines() if line.startswith("hits.txt")]) == 2


@pytest.mark.asyncio
async def test_run_tests_appends_selector_to_the_fixed_command(tmp_path: Path) -> None:
    tools = make_tools(tmp_path)
    outcome = await call_bench_tool(
        tools.get("run_tests"), {"selector": "tests/unit/x.py -k login"}, workspace=tmp_path
    )
    assert outcome.kind == "ok"
    # 基底命令不可替换，selector 只能追加在后面。
    assert "argv: ['tests/unit/x.py', '-k', 'login']" in outcome.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector",
    [
        "--rootdir=/",
        "--confcutdir=..",
        "-p no:cacheprovider",
        "..",
        "../outside.py",
        "tests/../outside.py",
        "/etc/passwd",
    ],
)
async def test_run_tests_rejects_selector_outside_allowlist(
    tmp_path: Path, selector: str
) -> None:
    tools = make_tools(tmp_path)
    outcome = await call_bench_tool(
        tools.get("run_tests"), {"selector": selector}, workspace=tmp_path
    )
    assert outcome.kind == "rejected"


def test_parse_selector_accepts_known_flags_and_relative_paths() -> None:
    tokens, reason = parse_selector("tests/unit/test_x.py::test_y -k login -x")
    assert reason is None
    assert tokens == ["tests/unit/test_x.py::test_y", "-k", "login", "-x"]


def test_parse_selector_rejects_unknown_flag() -> None:
    tokens, reason = parse_selector("--import-mode=importlib")
    assert tokens == []
    assert reason is not None


@pytest.mark.parametrize("selector", ["..", "../outside", "tests/../outside"])
def test_parse_selector_rejects_parent_directory_components(selector: str) -> None:
    tokens, reason = parse_selector(selector)

    assert tokens == []
    assert reason is not None
    assert "上级目录" in reason
