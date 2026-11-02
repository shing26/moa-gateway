"""用本仓库历史提交做回放的测试，需要检出目录**自带** ``.git``。

受治理交付的验证副本刻意不含 ``.git``（验证进程读不到 git 元数据）。更麻烦的是：
副本若落在别的仓库目录树里，``git`` 会沿父目录发现那个仓库，``git archive <历史提交>``
返回"空 tar + 退出码 0"，测试最后以 ``tarfile.ReadError`` 收场——看起来像产品缺陷，
实际是环境不具备前提。

CI 用 ``fetch-depth: 0`` 检出，检出目录自带 ``.git``，所以那边这些测试照常跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

requires_repository_history = pytest.mark.skipif(
    not (ROOT / ".git").exists(),
    reason="验证副本不含 .git：历史提交回放需要检出目录自带的 git 元数据",
)

__all__ = ["requires_repository_history"]
