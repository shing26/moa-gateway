"""并行专家的写隔离、冲突检测与确定性合并。"""

from __future__ import annotations

import base64
import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.workspace_tools import resolve_in_workspace

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        ".bench-work",
        ".bench-experiments",
        ".scratch",
    }
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_map(root: Path) -> dict[str, bytes]:
    base = root.resolve()
    files: dict[str, bytes] = {}
    for item in sorted(base.rglob("*")):
        relative = item.relative_to(base)
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        if item.is_symlink() or not item.is_file():
            continue
        files[relative.as_posix()] = item.read_bytes()
    return files


@dataclass(frozen=True)
class IsolatedWorkspace:
    source: Path
    root: Path
    _temporary_parent: Path

    def cleanup(self) -> None:
        shutil.rmtree(self._temporary_parent, ignore_errors=True)


def create_isolated_workspace(source: Path | str) -> IsolatedWorkspace:
    base = Path(source).resolve()
    if not base.is_dir():
        raise ValueError(f"隔离工作区源目录不存在: {base}")
    parent = Path(tempfile.mkdtemp(prefix="moa-workspace-"))
    target = parent / "workspace"
    try:
        shutil.copytree(
            base,
            target,
            ignore=shutil.ignore_patterns(*_SKIP_DIRS),
        )
    except Exception:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    return IsolatedWorkspace(
        source=base,
        root=target,
        _temporary_parent=parent,
    )


def collect_workspace_write_set(
    *,
    source: Path | str,
    isolated_root: Path | str,
    writer_id: str,
    task: str,
    round_index: int,
    accepted: bool = True,
) -> dict[str, Any]:
    """比较源工作区与隔离副本，产出可 JSON 落盘的独立写集。"""
    before = _file_map(Path(source))
    after = _file_map(Path(isolated_root))
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        before_hash = _sha256(old) if old is not None else ""
        after_hash = _sha256(new) if new is not None else ""
        change: dict[str, Any] = {
            "path": path,
            "kind": (
                "added"
                if old is None
                else "deleted"
                if new is None
                else "modified"
            ),
            "before_hash": before_hash,
            "after_hash": after_hash,
        }
        if new is not None:
            change["content_b64"] = base64.b64encode(new).decode("ascii")
        changes.append(change)
    return {
        "write_id": writer_id,
        "writer": writer_id,
        "task": task,
        "round": int(round_index),
        "accepted": bool(accepted),
        "changes": changes,
    }


@dataclass(frozen=True)
class MergeResult:
    applied: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...]
    settled_write_ids: tuple[str, ...]


def merge_workspace_write_sets(
    target_root: Path | str,
    write_sets: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    settled_write_ids: tuple[str, ...] | list[str] = (),
) -> MergeResult:
    """确定性合并写集；同一路径的分歧写入保留原文件并形成冲突证据。"""
    root = Path(target_root).resolve()
    settled = {str(item) for item in settled_write_ids}
    accepted = [
        item
        for item in write_sets
        if bool(item.get("accepted", True))
        and str(item.get("write_id", "")) not in settled
    ]
    all_new_ids = tuple(
        sorted(
            str(item.get("write_id", ""))
            for item in write_sets
            if str(item.get("write_id", "")) not in settled
        )
    )
    candidates: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for write_set in accepted:
        for change in write_set.get("changes") or ():
            if not isinstance(change, dict):
                continue
            path = str(change.get("path", "") or "")
            if path:
                candidates.setdefault(path, []).append((write_set, change))

    applied: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for path in sorted(candidates):
        items = candidates[path]
        candidates_public = [
            {
                "writer": str(write_set.get("writer", "")),
                "write_id": str(write_set.get("write_id", "")),
                "kind": str(change.get("kind", "")),
                "before_hash": str(change.get("before_hash", "")),
                "after_hash": str(change.get("after_hash", "")),
            }
            for write_set, change in sorted(
                items, key=lambda item: str(item[0].get("write_id", ""))
            )
        ]
        target = resolve_in_workspace(root, path)
        current_hash = (
            _sha256(target.read_bytes())
            if target.is_file()
            else ""
        )
        before_hashes = {
            str(change.get("before_hash", "")) for _, change in items
        }
        if before_hashes != {current_hash}:
            conflicts.append(
                {
                    "path": path,
                    "reason": "stale_base",
                    "before_hash": min(before_hashes)
                    if len(before_hashes) == 1
                    else "",
                    "current_hash": current_hash,
                    "candidates": candidates_public,
                }
            )
            continue
        after_hashes = {
            str(change.get("after_hash", "")) for _, change in items
        }
        if len(after_hashes) > 1:
            conflicts.append(
                {
                    "path": path,
                    "reason": "concurrent_divergent_write",
                    "before_hash": current_hash,
                    "current_hash": current_hash,
                    "candidates": candidates_public,
                }
            )
            continue

        # 同一 after 内容只落一次；writer 顺序由 write_id 排序固定。
        _, chosen = min(
            items, key=lambda item: str(item[0].get("write_id", ""))
        )
        content = chosen.get("content_b64")
        if content is None:
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(str(content)))
        applied.append(
            {
                "path": path,
                "kind": str(chosen.get("kind", "")),
                "writers": sorted(
                    {
                        str(write_set.get("writer", ""))
                        for write_set, _ in items
                    }
                ),
                "write_ids": sorted(
                    {
                        str(write_set.get("write_id", ""))
                        for write_set, _ in items
                    }
                ),
                "before_hash": current_hash,
                "after_hash": str(chosen.get("after_hash", "")),
            }
        )
    return MergeResult(
        applied=tuple(applied),
        conflicts=tuple(conflicts),
        settled_write_ids=tuple(sorted(set(all_new_ids))),
    )


def summarize_write_sets(
    write_sets: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    """去掉文件正文，只保留 Run Record 需要的写入证据。"""
    summaries: list[dict[str, Any]] = []
    for write_set in write_sets:
        summaries.append(
            {
                "write_id": str(write_set.get("write_id", "")),
                "writer": str(write_set.get("writer", "")),
                "task": str(write_set.get("task", "")),
                "round": int(write_set.get("round", 0) or 0),
                "accepted": bool(write_set.get("accepted", True)),
                "changes": [
                    {
                        "path": str(change.get("path", "")),
                        "kind": str(change.get("kind", "")),
                        "before_hash": str(change.get("before_hash", "")),
                        "after_hash": str(change.get("after_hash", "")),
                    }
                    for change in write_set.get("changes") or ()
                    if isinstance(change, dict)
                ],
            }
        )
    return sorted(summaries, key=lambda item: item["write_id"])


__all__ = [
    "IsolatedWorkspace",
    "MergeResult",
    "collect_workspace_write_set",
    "create_isolated_workspace",
    "merge_workspace_write_sets",
    "summarize_write_sets",
]
