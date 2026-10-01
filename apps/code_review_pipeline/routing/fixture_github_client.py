"""离线 GitHub 通道：让 golden path 在没有 token、没有外网的机器上真跑一遍（D5）。

**它替代的只有 GitHub 那一侧**。验签、幂等、队列、认领、状态机、审计、写回判定
全都走真实路径——被替掉的只有"去 api.github.com 拉 diff"和"在 PR 上贴评论"这两件
必须联网的事。所以它验证的是这套底座，不是 GitHub。

**为什么不把它做成测试里的 mock**：D2/D3 的教训已经吃过一次——假连接让"参数真的
生效"这类缺陷整整两轮测试全绿。golden path 的意义就在于端到端，所以离线的部分也
必须是真的读文件、真的写文件（`list_reviews` 读的是上一步 `create_review` 写下去
的那份 JSON），而不是一个内存字典。

开关是 ``CODE_REVIEW_GITHUB_FIXTURE``（指向 fixture JSON 的路径），**默认空**。
它必须显式打开：这条通道会让"写回成功"变成假的，绝不能被误当成生产配置。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any


def _store_path() -> str:
    """review 落盘位置。与 fixture 分开：fixture 是输入，这个是输出。"""
    return os.getenv("CODE_REVIEW_GITHUB_FIXTURE_STORE") or "data/demo_reviews.json"


class FixtureGitHubClient:
    """实现 ``GitHubClient`` 的那四个方法，读写本地 JSON。

    不继承 ``GitHubClient``：父类 ``__init__`` 会建一个带 base_url 的
    ``httpx.AsyncClient``，而这条通道一个请求都不该发出去。继承会让"离线"这件事
    在代码上看起来不成立。
    """

    def __init__(self, fixture_path: str, store_path: str | None = None) -> None:
        self._fixture_path = fixture_path
        self._store_path = store_path or _store_path()
        self._lock = threading.Lock()

    async def aclose(self) -> None:
        return None

    def _pull(self, repo: str, pr_number: int) -> dict[str, Any]:
        with open(self._fixture_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        pulls = data.get("pulls", {})
        key = f"{repo}#{int(pr_number)}"
        if key not in pulls:
            raise LookupError(f"fixture has no pull request {key}")
        return dict(pulls[key])

    def _read_store(self) -> dict[str, list[dict[str, Any]]]:
        if not os.path.exists(self._store_path):
            return {}
        with open(self._store_path, "r", encoding="utf-8") as fh:
            return dict(json.load(fh))

    def _write_store(self, data: dict[str, list[dict[str, Any]]]) -> None:
        parent = os.path.dirname(self._store_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    @staticmethod
    def _split(repo: Any) -> tuple[str, str]:
        owner, _, name = str(getattr(repo, "owner", "")).partition("/")
        return owner, name or str(getattr(repo, "name", ""))

    async def get_pr(self, repo: Any, pr_number: int) -> dict[str, Any]:
        owner, name = self._split(repo)
        return dict(self._pull(f"{owner}/{name}", pr_number).get("pr", {}))

    async def get_pr_files(self, repo: Any, pr_number: int) -> list[dict[str, Any]]:
        owner, name = self._split(repo)
        return list(self._pull(f"{owner}/{name}", pr_number).get("files", []))

    async def list_reviews(self, repo: Any, pr_number: int) -> list[dict[str, Any]]:
        """读**上一次写回真正落盘**的评论，不是 fixture 里的种子数据。

        D4 的兜底判据（扫 marker）就建立在这个列表上，所以它必须是写路径的
        真实产物——否则"重复审批只发一条"这条判据在离线模式下等于没验。
        """
        owner, name = self._split(repo)
        key = f"{owner}/{name}#{int(pr_number)}"
        return list(self._read_store().get(key, []))

    async def create_review(
        self, repo: Any, pr_number: int, body: str, *, event: str = "COMMENT"
    ) -> dict[str, Any]:
        owner, name = self._split(repo)
        key = f"{owner}/{name}#{int(pr_number)}"
        with self._lock:
            store = self._read_store()
            rows = list(store.get(key, []))
            # id 从现有条数推导：单进程 demo 下够用，且让"第二条评论"在 JSON 里
            # 一眼可辨（这正是重复写回时要人眼确认的东西）。
            new_id = str(1000 + len(rows) + 1)
            rows.append(
                {"id": int(new_id), "event": event, "body": body, "user": {"login": "moa-demo"}}
            )
            store[key] = rows
            self._write_store(store)
        return {"id": int(new_id), "event": event}

