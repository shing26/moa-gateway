"""变更包：agent 改动的纯数据表示（ADR-030 决策 2）。

变更包不含任何远端副作用。它记录 base sha、分支名、文件级 diff、patch 哈希与
验证结果；推送决策由管线在验证与审批之后单独做。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.agents.subprocess_env import harden_git_argv, isolated_env
from typing import Any

_MAX_DIFF_CHARS = 200_000

# 验证命令会留下缓存与报告产物。它们不是 agent 的改动，进变更包会让 PR 带上
# 二进制垃圾，也会让 patch_hash 每次运行都不同（不可复现）。
_IGNORED_PARTS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "node_modules",
        ".venv",
        "venv",
    }
)
_IGNORED_SUFFIXES = (".pyc", ".pyo", ".coverage", ".orig", ".rej")


def _is_ignored(path: str) -> bool:
    parts = Path(path).parts
    if any(part in _IGNORED_PARTS for part in parts):
        return True
    return path.endswith(_IGNORED_SUFFIXES)


@dataclass(frozen=True)
class FileChange:
    path: str
    kind: str  # added / modified / deleted
    before_hash: str
    after_hash: str
    mode: str = "100644"

    def __post_init__(self) -> None:
        if self.mode not in {"100644", "100755", "120000"}:
            raise ValueError(f"unsupported Git file mode: {self.mode!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class ChangeBundle:
    """一次 agent 变更的完整证据，可 JSON 落盘、可哈希、可复核。"""

    base_sha: str
    branch: str
    title: str
    body: str
    files: tuple[FileChange, ...]
    patch: str
    patch_hash: str
    verify_command: tuple[str, ...]
    verify_exit_code: int
    verify_output: str
    model: str
    trace_id: str = ""
    verify_output_hash: str = ""

    @property
    def is_verified(self) -> bool:
        return self.verify_exit_code == 0 and bool(self.files)

    @property
    def bundle_hash(self) -> str:
        # Approval binds the exported content and verification provenance.
        # Canonical JSON avoids delimiter ambiguity and ordering-dependent hashes.
        payload = {
            "base_sha": self.base_sha,
            "branch": self.branch,
            "title": self.title,
            "body": self.body,
            "files": [item.to_dict() for item in self.files],
            "patch_hash": self.patch_hash,
            "verify_command": list(self.verify_command),
            "verify_exit_code": self.verify_exit_code,
            "verify_output_hash": self.verify_output_hash,
            "model": self.model,
            "trace_id": self.trace_id,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_sha": self.base_sha,
            "branch": self.branch,
            "title": self.title,
            "body": self.body,
            "files": [item.to_dict() for item in self.files],
            "patch": self.patch,
            "patch_hash": self.patch_hash,
            "verify_command": list(self.verify_command),
            "verify_exit_code": self.verify_exit_code,
            "verify_output": self.verify_output,
            "verify_output_hash": self.verify_output_hash,
            "model": self.model,
            "trace_id": self.trace_id,
            "bundle_hash": self.bundle_hash,
        }


def verification_output_hash(stdout: str, stderr: str) -> str:
    payload = json.dumps(
        {"stdout": stdout, "stderr": stderr},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _sha256_text(payload)


def verification_output_excerpt(stdout: str, stderr: str) -> str:
    separator = "\n" if stdout and stderr else ""
    return f"{stdout}{separator}{stderr}"[-4000:]


def _git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 参数由本模块构造，不经 shell
        harden_git_argv(args),
        cwd=str(cwd),
        env=isolated_env(cwd, git=True),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _symlink_content(path: Path) -> bytes:
    return str(os.readlink(path)).encode("utf-8", errors="surrogateescape")


def _working_tree_mode(
    path: Path,
    *,
    base_mode: str,
    index_mode: str,
) -> str:
    if path.is_symlink():
        return "120000"
    if os.name != "nt" and path.stat().st_mode & 0o111:
        return "100755"
    if index_mode in {"100644", "100755"}:
        return index_mode
    return "100755" if base_mode.startswith("1007") else "100644"


def _content_hash(path: Path, mode: str) -> str:
    content = (
        _symlink_content(path)
        if mode == "120000"
        else path.read_bytes()
    )
    return _sha256_bytes(content)


def build_change_bundle(
    *,
    workspace: Path,
    base_sha: str,
    branch: str,
    title: str,
    body: str,
    verify_command: tuple[str, ...],
    verify_exit_code: int,
    verify_output: str,
    model: str,
    trace_id: str = "",
    verify_output_hash: str = "",
) -> ChangeBundle:
    """从工作区采集 diff 与文件清单，组装变更包。不触碰远端。"""
    root = Path(workspace).resolve()
    status = _git(
        ["status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        check=False,
    ).stdout
    untracked = [
        line[3:].strip().strip('"')
        for line in status.splitlines()
        if line.startswith("??") and not _is_ignored(line[3:].strip().strip('"'))
    ]
    if untracked:
        # intent-to-add 让新增文件进入 `git diff HEAD`；不实际暂存内容，
        # 工作区语义保持不变。
        _git(["add", "-N", "--", *untracked], cwd=root, check=False)

    # --no-color 保证 diff 可哈希；--binary 让二进制改动也出现在 patch 里。
    full_diff = _git(
        [
            "diff",
            "--no-color",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
        ],
        cwd=root,
        check=False,
    ).stdout
    diff = full_diff
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + "\n…（diff 已截断）"

    before_hashes = _git(
        ["ls-tree", "-r", "-z", "--full-tree", "HEAD"],
        cwd=root,
        check=False,
    ).stdout
    base_blobs: dict[str, str] = {}
    base_modes: dict[str, str] = {}
    for row in before_hashes.split("\0"):
        meta, separator, path = row.partition("\t")
        parts = meta.split()
        if separator and len(parts) >= 3 and parts[1] == "blob":
            base_blobs[path] = parts[2]
            base_modes[path] = parts[0]

    index_modes: dict[str, str] = {}
    for row in _git(
        ["ls-files", "--stage", "-z"],
        cwd=root,
        check=False,
    ).stdout.split("\0"):
        meta, separator, path = row.partition("\t")
        parts = meta.split()
        if separator and len(parts) >= 2:
            index_modes[path] = parts[0]

    files: list[FileChange] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip() or "??"
        path = line[3:].strip().strip('"')
        if _is_ignored(path):
            continue
        kind = (
            "added"
            if code in {"??", "A", "AM"}
            else "deleted"
            if "D" in code
            else "modified"
        )
        target = root / path
        if kind == "deleted":
            mode = base_modes.get(path, "100644")
            after_hash = ""
        else:
            mode = _working_tree_mode(
                target,
                base_mode=base_modes.get(path, ""),
                index_mode=index_modes.get(path, ""),
            )
            after_hash = _content_hash(target, mode)
        files.append(
            FileChange(
                path=path,
                kind=kind,
                before_hash=base_blobs.get(path, ""),
                after_hash=after_hash,
                mode=mode,
            )
        )

    return ChangeBundle(
        base_sha=base_sha,
        branch=branch,
        title=title,
        body=body,
        files=tuple(files),
        patch=diff,
        patch_hash=_sha256_text(full_diff),
        verify_command=verify_command,
        verify_exit_code=verify_exit_code,
        verify_output=verify_output,
        model=model,
        trace_id=trace_id,
        verify_output_hash=verify_output_hash,
    )
