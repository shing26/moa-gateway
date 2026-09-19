"""长期记忆生命周期。

短期会话记忆由 ``app/memory.py`` 的 ``RedisConversationStorage`` 承担：按
``session_id`` 隔离、带 TTL、进程或会话结束即消失。本模块补的是它缺的那一层：
值得长期保留的事实 -> 抽取 -> 落向量库 -> 下次会话注入 -> 冲突更新 / 遗忘。

落库复用 ``VectorStore`` 协议（内存后端与 pgvector 后端都实现），因此本模块不
感知具体后端，也不需要新的存储依赖。检索过滤固定带 ``source`` 与 ``user_id``，
从存储层面保证不同用户之间的记忆不可串号。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.vectordb import VectorDocument, VectorStore

# 记忆槽位归一化：同一件事的不同说法要落到同一个 key，才能实现"冲突更新"而不是
# 追加两条互相矛盾的事实。未收录的标签按字面量作 key（大小写、空白归一）。
_KEY_ALIASES = {
    "名字": "name",
    "姓名": "name",
    "name": "name",
    "邮箱": "email",
    "邮箱地址": "email",
    "email": "email",
    "电话": "phone",
    "手机号": "phone",
    "手机": "phone",
    "phone": "phone",
    "生日": "birthday",
    "出生日期": "birthday",
    "birthday": "birthday",
    "地址": "address",
    "address": "address",
    "项目名称": "project",
    "项目": "project",
    "project": "project",
    "截止日期": "deadline",
    "截止时间": "deadline",
    "项目截止日期": "deadline",
    "项目截止时间": "deadline",
    "deadline": "deadline",
    "公司": "company",
    "职位": "title",
    "岗位": "title",
}


@dataclass(frozen=True)
class MemoryOp:
    """一次记忆操作。``action`` 取 ``remember`` / ``forget`` / ``forget_all``。"""

    action: str
    key: str = ""
    label: str = ""
    value: str = ""


_REMEMBER_ZH = re.compile(
    r"(?:请|麻烦)?(?:帮我)?记住[，,：:\s]*我?的?"
    r"(?P<label>[A-Za-z_][A-Za-z0-9_ ]{0,19}|[\u4e00-\u9fff]{1,12}?)"
    r"(?:叫做|叫|是|为|：|:)\s*(?P<value>[^。！!.\n]+)"
)
_REMEMBER_EN = re.compile(
    r"(?:please\s+)?remember\s+(?:that\s+)?my\s+"
    r"(?P<label>[A-Za-z_][A-Za-z0-9_ ]{0,19}?)\s+(?:is|are)\s+(?P<value>[^.!?\n]+)",
    re.IGNORECASE,
)
_FORGET_ALL = re.compile(
    r"(?:请|麻烦)?(?:帮我)?(?:忘掉|忘记|删除|清除|移除)我?的?(?:所有|全部)?(?:长期)?记忆"
)
_FORGET_ZH = re.compile(
    r"(?:请|麻烦)?(?:帮我)?(?:忘掉|忘记|删除|清除|移除)(?:关于)?我?的?"
    r"(?P<label>[A-Za-z_][A-Za-z0-9_ ]{0,19}|[\u4e00-\u9fff]{1,12}?)"
    r"(?:的)?(?:记忆|信息|记录|资料)?\s*[。.!！]?\s*$"
)
_FORGET_EN = re.compile(
    r"(?:please\s+)?(?:forget|delete|remove)\s+my\s+"
    r"(?P<label>[A-Za-z_][A-Za-z0-9_ ]{0,19}?)"
    r"(?:\s+(?:memory|info|information|record))?\s*[.!?]?\s*$",
    re.IGNORECASE,
)


def normalize_key(label: str) -> str:
    """把用户口语标签收敛成稳定的记忆槽位 key。"""

    cleaned = re.sub(r"[\s:：]+", "", label or "").strip().lower()
    return _KEY_ALIASES.get(cleaned, cleaned)


def extract_memory_ops(text: str) -> list[MemoryOp]:
    """从一句话里抽取显式记忆指令。

    只识别显式表达（"记住…" / "忘记…"），不做隐式推断。让一个 0.5B 级本地模型
    逐句自动抽取会把噪声塞进长期库、还拖慢每次请求，收益不划算。
    """

    if not text:
        return []

    if _FORGET_ALL.search(text):
        return [MemoryOp(action="forget_all")]

    for pattern in (_FORGET_ZH, _FORGET_EN):
        match = pattern.search(text)
        if match:
            label = match.group("label").strip()
            key = normalize_key(label)
            if key:
                return [MemoryOp(action="forget", key=key, label=label)]

    for pattern in (_REMEMBER_ZH, _REMEMBER_EN):
        match = pattern.search(text)
        if match:
            label = match.group("label").strip()
            value = match.group("value").strip().strip("，,。.！!?？\"'")
            key = normalize_key(label)
            if key and value:
                return [MemoryOp(action="remember", key=key, label=label, value=value)]

    return []


class LongTermMemory:
    """封装在 ``VectorStore`` 之上的长期记忆读写。"""

    SOURCE = "long_term_memory"
    DEFAULT_TOP_K = 3

    def __init__(self, client: VectorStore, top_k: int = DEFAULT_TOP_K) -> None:
        self._client = client
        self._top_k = top_k

    @staticmethod
    def doc_id(user_id: str, key: str) -> str:
        return f"ltm:{user_id}:{key}"

    @staticmethod
    def _content(label: str, key: str, value: str) -> str:
        # 同时写入原标签与归一化 key：中文查询命中"名字"，英文查询命中"name"。
        if label and label.lower() != key:
            return f"{label} ({key}): {value}"
        return f"{label or key}: {value}"

    async def remember(
        self,
        user_id: str,
        key: str,
        value: str,
        *,
        label: str = "",
        session_id: str | None = None,
    ) -> str:
        """写入或覆盖一个记忆槽位，返回该槽位 key。"""

        label = label or key
        doc = VectorDocument(
            id=self.doc_id(user_id, key),
            content=self._content(label, key, value),
            metadata={
                "source": self.SOURCE,
                "user_id": user_id,
                "memory_key": key,
                "session_id": session_id or "",
            },
        )
        await self._client.upsert(doc)
        return key

    async def recall(self, query: str, user_id: str, top_k: int | None = None) -> list[VectorDocument]:
        """召回该用户与查询相关的记忆条目。"""

        if not user_id:
            return []
        result = await self._client.search(
            query,
            top_k=top_k or self._top_k,
            filter_metadata={"source": self.SOURCE, "user_id": user_id},
        )
        return [doc for doc in result.documents if doc.score > 0]

    async def recall_context(self, query: str, user_id: str, top_k: int | None = None) -> str:
        docs = await self.recall(query, user_id, top_k=top_k)
        return "\n".join(doc.content for doc in docs)

    async def list_for(self, user_id: str) -> list[VectorDocument]:
        if not user_id:
            return []
        return await self._client.find_by_metadata(
            {"source": self.SOURCE, "user_id": user_id}, limit=100
        )

    async def forget(self, user_id: str, key: str) -> int:
        """删除单个槽位，返回删除条数（0 表示本来就没有）。"""

        return await self._client.delete_by_metadata(
            {"source": self.SOURCE, "user_id": user_id, "memory_key": key}
        )

    async def forget_all(self, user_id: str) -> int:
        return await self._client.delete_by_metadata(
            {"source": self.SOURCE, "user_id": user_id}
        )

    async def apply_ops(
        self,
        user_id: str,
        ops: list[MemoryOp],
        *,
        session_id: str | None = None,
    ) -> list[str]:
        """执行抽取出来的操作，返回可读的执行结果，便于日志与测试断言。"""

        if not user_id:
            return []
        applied: list[str] = []
        for op in ops:
            if op.action == "remember":
                await self.remember(
                    user_id, op.key, op.value, label=op.label, session_id=session_id
                )
                applied.append(f"remembered:{op.key}")
            elif op.action == "forget":
                count = await self.forget(user_id, op.key)
                applied.append(f"forgotten:{op.key}:{count}")
            elif op.action == "forget_all":
                count = await self.forget_all(user_id)
                applied.append(f"forgotten_all:{count}")
        return applied

    async def apply(self, user_id: str, text: str, *, session_id: str | None = None) -> list[str]:
        return await self.apply_ops(
            user_id, extract_memory_ops(text), session_id=session_id
        )


__all__ = ["LongTermMemory", "MemoryOp", "extract_memory_ops", "normalize_key"]
