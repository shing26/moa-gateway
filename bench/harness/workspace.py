"""工作区的物化、快照、patch 采集与测试文件还原。

为什么是 orphan 单提交快照
--------------------------
§7.6 要求"剥掉 git 历史"，因为 E1 的修复就在仓库历史里，带了 ``.git`` 的 agent
可以 ``git log`` / ``git show`` 把答案抄出来。但完全不要 ``.git`` 又拿不到
``git diff``（patch 是判决的证据）。折中就是 §7.6 明确允许的另一种形式：
**orphan 快照**——工作区里有 ``.git``，但只有一个提交，内容恰好是
"基线 + test patch"。修复不在里面，而 ``git diff`` 照常可用。

还原的目标是 test patch 版本，不是基线版本
------------------------------------------
§7.2 原本写"还原到基线版本"，但基线里**没有这个测试**（实测：``test_auth.py``
在基线是 140 行，只断言 ``/webhook`` 与 ``/dashboard``，相关断言是修复 commit
新增的）。还原到基线等于把判据删掉。所以还原目标是工作区那个 orphan 提交，
也就是 test patch 版本。

还原的粒度是**整棵测试树**，不是 ``test_paths`` 里那几个文件
------------------------------------------------------------
实测（2026-10-17 review）：``tests/conftest.py`` 在基线里就是**已跟踪**文件，agent
改它是"修改"而非"新增"，``git clean`` 碰不到；而它又不是 ``test_paths``，``git
checkout`` 也碰不到。只按 ``test_paths`` 还原，等于留了一条改了收集钩子还能过判据
的路。所以按测试树根整棵还原（checkout + clean），代价是 ``test_paths`` 必须落在
一个专用测试树里，树里不应有修复本身需要的源文件。
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

logger = logging.getLogger("bench.harness.workspace")

#: ``tarfile`` 的 ``filter="data"`` 会对每个成员调一次 ``os.path.realpath``；在
#: Windows 上这一句偶发 ``OSError: [WinError 1450] 系统资源不足``（curve 实测第
#: 三条 run 上炸掉，前两条正常）。它是环境抖动而不是数据问题，重试一次即可；
#: 重试前必须清空半成品目录，否则第二次会撞"文件已存在"。
_EXTRACT_ATTEMPTS = 3
_EXTRACT_BACKOFF_S = 0.5


class MissingCommitError(RuntimeError):
    """基线 commit 不在源仓库里。浅克隆（CI 的默认行为）就会这样。"""


def git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 参数由本模块构造，不经 shell
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _git_bytes(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=str(cwd), capture_output=True, check=False
    )


def _extract_archive(data: bytes, dest: Path) -> None:
    """把 ``git archive`` 的 tar 展开到 ``dest``，抖动时有界重试。"""
    for attempt in range(1, _EXTRACT_ATTEMPTS + 1):
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                # filter="data" 会丢弃绝对路径、``..`` 与设备文件等危险成员。
                tar.extractall(dest, filter="data")
            return
        except OSError as exc:
            if attempt >= _EXTRACT_ATTEMPTS:
                raise
            logger.warning(
                "tar extraction failed (attempt %d/%d): %s; retrying",
                attempt,
                _EXTRACT_ATTEMPTS,
                exc,
            )
            for child in dest.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            time.sleep(_EXTRACT_BACKOFF_S * attempt)


def materialize(
    *,
    repo: Path,
    base_commit: str,
    test_patch: Path,
    dest: Path,
    allow_empty_patch: bool = False,
) -> Path:
    """把"基线 + test patch"物化成一个带 orphan 快照的工作区，返回其根目录。"""
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"工作区目录非空: {dest}")
    dest.mkdir(parents=True, exist_ok=True)

    # 必须显式关掉换行转换：`git archive` 会按 ``core.autocrlf`` 做 LF→CRLF 的
    # 导出转换，于是工作区里是 CRLF、而 test patch 的上下文是 LF（git diff 输出的
    # 永远是 blob 原文），``git apply`` 会以 "patch does not apply" 收场。本机全局
    # autocrlf=true，实测踩到。关掉它，导出就与 blob 逐字节一致。
    archive = _git_bytes(
        [
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "archive",
            "--format=tar",
            base_commit,
        ],
        cwd=repo,
    )
    if archive.returncode != 0:
        raise MissingCommitError(
            f"git archive {base_commit} 失败（提交可能不在本地，浅克隆会这样）"
        )
    # ``_extract_archive`` 里 filter="data" 会丢弃绝对路径、`..` 与设备文件等危险
    # 成员。pyproject 声明 requires-python >= 3.12，而这个参数 3.12 起是默认安全
    # 策略的一部分，所以不做旧解释器回退——留着回退只会让"安全版本"变成一条不被
    # 走到的分支。
    _extract_archive(archive.stdout, dest)

    git(["init", "-q", "-b", "main"], cwd=dest)
    # Windows 上默认的 autocrlf 会把 LF 换成 CRLF，于是"agent 改了一个字节"
    # 会变成整文件重写——patch 立刻变成噪音。关掉它。
    git(["config", "core.autocrlf", "false"], cwd=dest)
    git(["config", "user.email", "bench@localhost"], cwd=dest)
    git(["config", "user.name", "bench"], cwd=dest)

    apply_argv = ["apply", "--whitespace=nowarn"]
    if allow_empty_patch:
        apply_argv.append("--allow-empty")
    apply_argv.append(str(test_patch))
    applied = git(apply_argv, cwd=dest, check=False)
    if applied.returncode != 0:
        raise RuntimeError(f"test patch 无法应用到基线: {applied.stderr.strip()}")

    git(["add", "-A"], cwd=dest)
    git(["commit", "-q", "--no-gpg-sign", "-m", "baseline+test"], cwd=dest)
    logger.info("workspace materialized at %s", dest)
    return dest


def collect_patch(workspace: Path) -> str:
    """采集 agent 的全部改动（含新增文件）。先暂存再 diff 是为了带上空文件。"""
    git(["add", "-A"], cwd=workspace)
    diff = git(["diff", "--cached", "--no-color", "--no-ext-diff"], cwd=workspace)
    # 还原本模块对索引的改动，免得影响后面的 checkout HEAD。
    git(["reset", "-q"], cwd=workspace, check=False)
    return diff.stdout


def restore_test_files(workspace: Path, test_paths: tuple[str, ...]) -> None:
    """把整棵测试树还原到 orphan 提交的版本，并清掉树里新增的未跟踪文件。"""
    if not test_paths:
        return
    git(["checkout", "HEAD", "--", *test_paths], cwd=workspace, check=False)
    roots = sorted({Path(item).parts[0] for item in test_paths if Path(item).parts})
    for root in roots:
        if root in ("", "."):
            continue
        # 整棵树 checkout：conftest.py 之类的收集钩子不在 test_paths 里，却能改写判据。
        git(["checkout", "HEAD", "--", root], cwd=workspace, check=False)
        # 再清未跟踪文件：新增的测试文件同样会改变收集结果。
        git(["clean", "-qfd", "--", root], cwd=workspace, check=False)


def touched_test_paths(patch: str, test_paths: tuple[str, ...]) -> bool:
    """patch 是否碰过测试文件。§7.5 的第一信号，判据仍是还原后的复验。"""
    roots = {str(Path(item).parent.as_posix()) for item in test_paths}
    for path in changed_paths(patch):
        if path in test_paths or any(path.startswith(root + "/") for root in roots):
            return True
    return False


def changed_paths(patch: str) -> list[str]:
    """patch 改过的目标路径（``b/`` 侧），按出现顺序。

    解析 ``diff --git`` 头这件事只有这一个所有者——``touched_test_paths`` 的防作弊
    信号与测试里的 solution patch 文件清单都走它，免得两边各写一份 split。
    """
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split(" b/", 1)
        if len(parts) != 2:
            continue
        paths.append(parts[1].strip())
    return paths


def has_original_history(workspace: Path, base_commit: str) -> bool:
    """工作区里是否残留了原始历史。用于自检（应当恒为 False）。"""
    found = git(["cat-file", "-e", base_commit], cwd=workspace, check=False)
    if found.returncode == 0:
        return True
    log = git(["rev-list", "--count", "HEAD"], cwd=workspace, check=False)
    return (log.stdout or "").strip() not in ("", "1")


__all__ = [
    "MissingCommitError",
    "changed_paths",
    "collect_patch",
    "git",
    "has_original_history",
    "materialize",
    "restore_test_files",
    "touched_test_paths",
]
