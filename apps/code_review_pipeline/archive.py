"""GitHub 仓库归档的安全解压。"""

from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath


def extract_repo_tarball(archive: Path, dest: Path) -> None:
    """解压 GitHub tarball，并剥掉它自带的顶层目录。

    GitHub tarball 的形状是 ``owner-repo-sha/...``。工作区根应当直接是仓库内容，
    否则 agent 读 ``app/x.py`` 时会读到不存在的路径。所有成员都先做路径检查，
    再交给 ``filter="data"`` 处理链接与设备文件。
    """
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if not members:
            return
        top = PurePosixPath(members[0].name).parts[0]
        for member in members:
            if "\\" in member.name:
                raise ValueError(f"归档路径含反斜杠，拒绝: {member.name!r}")
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != top or len(parts) == 1:
                continue
            relative = PurePosixPath(*parts[1:])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"归档路径越界: {member.name!r}")
            member.name = relative.as_posix()
            tar.extract(member, path=dest, filter="data")


__all__ = ["extract_repo_tarball"]
