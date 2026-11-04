"""§5.0 工具层：工作区边界、上限，以及"筛选参数不能变成换判据"。"""

from __future__ import annotations

import os
import shutil
import subprocess
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


def test_verification_copy_rejects_workspace_symlinks(tmp_path: Path) -> None:
    from app.agents.workspace_tools import create_verification_workspace

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    try:
        os.symlink(outside, workspace / "linked-dir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(OSError, match="符号链接"):
        create_verification_workspace(workspace)

    assert not list(tmp_path.glob("moa-verify-*"))


def test_verification_copy_excludes_nested_cache_directories(
    tmp_path: Path,
) -> None:
    from app.agents.workspace_tools import create_verification_workspace

    workspace = tmp_path / "workspace"
    nested = workspace / "pkg" / "node_modules" / "dep"
    nested.mkdir(parents=True)
    (nested / "index.js").write_text("cached\n", encoding="utf-8")
    ruff_cache = workspace / "pkg" / ".ruff_cache"
    ruff_cache.mkdir()
    (ruff_cache / "cache").write_text("cached\n", encoding="utf-8")
    (workspace / "pkg" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    copied, parent = create_verification_workspace(workspace)
    try:
        assert (copied / "pkg" / "app.py").is_file()
        assert not (copied / "pkg" / "node_modules").exists()
        assert not (copied / "pkg" / ".ruff_cache").exists()
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_verification_copy_excludes_git_file_metadata(tmp_path: Path) -> None:
    from app.agents.workspace_tools import create_verification_workspace

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").write_text(
        "gitdir: /private/repository/.git/worktrees/example\n",
        encoding="utf-8",
    )
    (workspace / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    copied, parent = create_verification_workspace(workspace)
    try:
        assert (copied / "app.py").is_file()
        assert not (copied / ".git").exists()
    finally:
        shutil.rmtree(parent, ignore_errors=True)


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


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_baseline(workspace: Path) -> None:
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "bench@localhost")
    _git(workspace, "config", "user.name", "bench")
    _git(workspace, "add", "--all")
    _git(workspace, "commit", "-q", "--allow-empty", "-m", "baseline")


@pytest.mark.asyncio
async def test_replace_in_file_replaces_a_unique_match(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "VALUE = 1\nOTHER = 2\n", encoding="utf-8", newline="\n"
    )
    tools = make_tools(tmp_path)

    outcome = await call_bench_tool(
        tools.get("replace_in_file"),
        {"path": "app.py", "old": "VALUE = 1", "new": "VALUE = 3"},
        workspace=tmp_path,
    )

    assert outcome.kind == "ok"
    assert "已替换 app.py" in outcome.detail
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 3\nOTHER = 2\n"


@pytest.mark.asyncio
async def test_replace_in_file_rejects_an_ambiguous_match(tmp_path: Path) -> None:
    original = "x = 1\nx = 1\n"
    (tmp_path / "app.py").write_text(original, encoding="utf-8", newline="\n")
    tools = make_tools(tmp_path)

    outcome = await call_bench_tool(
        tools.get("replace_in_file"),
        {"path": "app.py", "old": "x = 1", "new": "x = 2"},
        workspace=tmp_path,
    )

    # 歧义是模型给的片段不够唯一，属于可自行修正的入参问题，不是环境失败。
    assert outcome.kind == "rejected"
    assert "2 处" in outcome.detail
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_replace_in_file_replace_all(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "x = 1\nx = 1\n", encoding="utf-8", newline="\n"
    )
    tools = make_tools(tmp_path)

    outcome = await call_bench_tool(
        tools.get("replace_in_file"),
        {"path": "app.py", "old": "x = 1", "new": "x = 2", "replace_all": True},
        workspace=tmp_path,
    )

    assert outcome.kind == "ok"
    assert "2 处" in outcome.detail
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 2\nx = 2\n"


@pytest.mark.asyncio
async def test_replace_in_file_rejects_a_missing_match(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    tools = make_tools(tmp_path)

    outcome = await call_bench_tool(
        tools.get("replace_in_file"),
        {"path": "app.py", "old": "VALUE = 9", "new": "VALUE = 3"},
        workspace=tmp_path,
    )

    assert outcome.kind == "rejected"
    assert "未找到" in outcome.detail
    assert "read_file" in outcome.detail


@pytest.mark.asyncio
async def test_replace_in_file_preserves_crlf_line_endings(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_bytes(b"alpha\r\nbeta\r\n")
    tools = make_tools(tmp_path)

    outcome = await call_bench_tool(
        tools.get("replace_in_file"),
        # 模型从 read_file 看到的是 LF 风格文本。
        {"path": "app.py", "old": "alpha\nbeta", "new": "gamma\nbeta"},
        workspace=tmp_path,
    )

    assert outcome.kind == "ok"
    assert (tmp_path / "app.py").read_bytes() == b"gamma\r\nbeta\r\n"


@pytest.mark.asyncio
async def test_replace_in_file_rejects_an_empty_old_string(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")

    outcome = await call_bench_tool(
        make_tools(tmp_path).get("replace_in_file"),
        {"path": "app.py", "old": "", "new": "x"},
        workspace=tmp_path,
    )

    assert outcome.kind == "rejected"
    assert "不能为空" in outcome.detail


@pytest.mark.asyncio
async def test_replace_in_file_rejects_path_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "bench-outside-edit.txt"
    outside.write_text("keep\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outcome = await call_bench_tool(
        make_tools(workspace).get("replace_in_file"),
        {"path": "../bench-outside-edit.txt", "old": "keep", "new": "changed"},
        workspace=workspace,
    )

    assert outcome.kind == "rejected"
    assert outside.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.asyncio
async def test_replace_in_file_does_not_follow_a_symlink_final_component(
    tmp_path: Path,
) -> None:
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
        make_tools(workspace).get("replace_in_file"),
        {"path": "linked.txt", "old": "keep", "new": "changed"},
        workspace=workspace,
    )

    assert outcome.kind == "rejected"
    assert outside.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.asyncio
async def test_replace_in_file_rejects_git_metadata(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret\n", encoding="utf-8")

    outcome = await call_bench_tool(
        make_tools(tmp_path).get("replace_in_file"),
        {"path": ".git/config", "old": "secret", "new": "changed"},
        workspace=tmp_path,
    )

    assert outcome.kind == "rejected"
    assert "Git 元数据" in outcome.detail


@pytest.mark.asyncio
async def test_git_diff_shows_working_tree_changes(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    _init_baseline(tmp_path)
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")

    outcome = await call_bench_tool(
        make_tools(tmp_path).get("git_diff"), {}, workspace=tmp_path
    )

    assert outcome.kind == "ok"
    assert "M app.py" in outcome.detail
    assert "-VALUE = 1" in outcome.detail
    assert "+VALUE = 2" in outcome.detail


@pytest.mark.asyncio
async def test_git_diff_stat_only_omits_the_patch(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    _init_baseline(tmp_path)
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")

    outcome = await call_bench_tool(
        make_tools(tmp_path).get("git_diff"), {"stat_only": True}, workspace=tmp_path
    )

    assert outcome.kind == "ok"
    assert "app.py |" in outcome.detail
    assert "+VALUE = 2" not in outcome.detail


@pytest.mark.asyncio
async def test_git_diff_lists_untracked_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    _init_baseline(tmp_path)
    (tmp_path / "new.py").write_text("NEW = 1\n", encoding="utf-8", newline="\n")

    outcome = await call_bench_tool(
        make_tools(tmp_path).get("git_diff"), {}, workspace=tmp_path
    )

    assert outcome.kind == "ok"
    assert "?? new.py" in outcome.detail
    assert "未跟踪文件" in outcome.detail


@pytest.mark.asyncio
async def test_git_diff_filters_by_path(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8", newline="\n")
    (tmp_path / "b.py").write_text("B = 1\n", encoding="utf-8", newline="\n")
    _init_baseline(tmp_path)
    (tmp_path / "a.py").write_text("A = 2\n", encoding="utf-8", newline="\n")
    (tmp_path / "b.py").write_text("B = 2\n", encoding="utf-8", newline="\n")

    outcome = await call_bench_tool(
        make_tools(tmp_path).get("git_diff"), {"path": "a.py"}, workspace=tmp_path
    )

    assert outcome.kind == "ok"
    assert "a.py" in outcome.detail
    assert "b.py" not in outcome.detail


@pytest.mark.asyncio
async def test_git_diff_without_a_baseline_repository_fails(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")

    outcome = await call_bench_tool(
        make_tools(tmp_path).get("git_diff"), {}, workspace=tmp_path
    )

    assert outcome.kind == "failed"
    assert "baseline" in outcome.detail


@pytest.mark.asyncio
async def test_git_diff_rejects_option_like_paths(tmp_path: Path) -> None:
    _init_baseline(tmp_path)

    outcome = await call_bench_tool(
        make_tools(tmp_path).get("git_diff"), {"path": "--stat"}, workspace=tmp_path
    )

    assert outcome.kind == "rejected"
    assert "不能以 - 或 : 开头" in outcome.detail
