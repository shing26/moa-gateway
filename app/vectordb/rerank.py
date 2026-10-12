"""检索精排（rerank）：在 RRF 融合之后、截断到 top_k 之前重排候选。

为什么需要它
------------
RRF 只用名次融合，免量纲、免调参，但它只能重排**两条腿已经召回**的候选。
当相关文档被召回但排在后面时（词法信号弱、或两条腿的名次都不高），RRF
无法把它提上来。精排器用一个更强的信号（交叉编码器或 LLM）对候选重新打分，
把最相关的排到前面。

三种实现
----------
* ``NoopReranker``：恒等，默认。未启用精排时行为零变化。
* ``HttpRerankProvider``：调本地/自托管的 OpenAI/Cohere 兼容 ``/rerank`` 端点。
  带熔断与优雅降级：失败时保留 RRF 顺序，绝不让检索报错。
* ``DeterministicLexicalReranker``：纯 Python 的 query token 覆盖率排序，供离线
  前后对比。它**不依赖网络**，因此 CI 能在零网络下跑 base vs rerank 的 delta。

接入点
------
``ContextRetriever`` 在 RRF 融合后、截断到 ``top_k`` 前调用精排器。这样
``VectorStore`` 协议不变、内存后端行为不变，blast radius 最小。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from app.vectordb import VectorDocument
from app.vectordb.keywords import query_tokens

logger = logging.getLogger("moa.vectordb.rerank")


class Reranker(Protocol):
    """精排器协议：对候选文档重新排序，返回 top_k 个。"""

    async def rerank(
        self, query: str, docs: list[VectorDocument], top_k: int
    ) -> list[VectorDocument]: ...


class NoopReranker:
    """恒等精排器：不改变顺序。默认实现，行为零变化。"""

    async def rerank(
        self, query: str, docs: list[VectorDocument], top_k: int
    ) -> list[VectorDocument]:
        return docs[:top_k]


class HttpRerankProvider:
    """调本地/自托管的 OpenAI/Cohere 兼容 ``/rerank`` 端点。

    与 ``EmbeddingProvider`` 同一套姿势：可选依赖、熔断、优雅降级。失败时
    返回原始顺序（保留 RRF 排序），绝不让检索报错——精排是增强，不是前提。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_s: float = 10.0,
        max_failures: int = 3,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_failures = max_failures
        self._failures = 0
        self._disabled = False
        self._last_error: str | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key) and not self._disabled

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_s,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def rerank(
        self, query: str, docs: list[VectorDocument], top_k: int
    ) -> list[VectorDocument]:
        if not docs or top_k <= 0:
            return docs[:top_k]
        if not self.enabled:
            return docs[:top_k]
        try:
            payload = await self._post(
                {
                    "model": self._model,
                    "query": query,
                    "documents": [doc.content for doc in docs],
                    "top_n": min(top_k, len(docs)),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 精排失败不拖垮检索
            self._record_failure(exc)
            return docs[:top_k]

        results = payload.get("results") or []
        if not results:
            return docs[:top_k]

        # Cohere 格式：results 是 [{"index": i, "relevance_score": s}, ...]
        scored: list[tuple[int, float]] = []
        for item in results:
            index = item.get("index")
            score = item.get("relevance_score")
            if isinstance(index, int) and 0 <= index < len(docs) and isinstance(score, (int, float)):
                scored.append((index, float(score)))
        if not scored:
            return docs[:top_k]

        scored.sort(key=lambda pair: pair[1], reverse=True)
        reranked = [docs[index] for index, _ in scored if 0 <= index < len(docs)]
        # 把没被精排器返回的文档附在后面（保底，不丢候选）
        seen = {id(doc) for doc in reranked}
        reranked.extend(doc for doc in docs if id(doc) not in seen)
        return reranked[:top_k]

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client_get().post(f"{self._base_url}/rerank", json=body)
        response.raise_for_status()
        return response.json()

    def _record_failure(self, exc: BaseException) -> None:
        self._failures += 1
        self._last_error = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning(
            "rerank 调用失败 (%d/%d): %s", self._failures, self._max_failures, self._last_error
        )
        if self._failures >= self._max_failures:
            self._disabled = True
            logger.error(
                "rerank 连续失败 %d 次，已熔断：本次检索保留 RRF 顺序，重启进程后恢复",
                self._max_failures,
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class DeterministicLexicalReranker:
    """纯 Python 的 query token 覆盖率排序，供离线前后对比。

    与 base 检索的 ``keyword_score``（出现次数加权）不同，这里用的是**覆盖率**：
    query 中有多少比例的 token 出现在 doc 里。两者信号不同，因此可以产生不同
    的排序，从而在 gold set 上展示 rerank 的 delta（无论正负）。

    它**不依赖网络**，因此 CI 能在零网络下跑 base vs rerank 的对比。
    """

    async def rerank(
        self, query: str, docs: list[VectorDocument], top_k: int
    ) -> list[VectorDocument]:
        if not docs or top_k <= 0:
            return docs[:top_k]
        tokens = set(query_tokens(query.lower()))
        if not tokens:
            return docs[:top_k]

        scored: list[tuple[float, str, VectorDocument]] = []
        for doc in docs:
            content_lower = doc.content.lower()
            hits = sum(1 for token in tokens if token in content_lower)
            coverage = hits / len(tokens)
            # 次级键用 doc.id 保证确定性（同分时顺序稳定）。
            scored.append((coverage, doc.id, doc))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [doc for _, _, doc in scored[:top_k]]


def build_reranker(settings_obj: Any | None = None) -> Reranker:
    """根据配置选择精排器。

    未启用或未配置端点时返回 ``NoopReranker``（恒等），行为零变化。
    """
    if settings_obj is None:
        from app.config import settings as settings_obj  # type: ignore[no-redef]

    if not getattr(settings_obj, "rerank_enabled", False):
        return NoopReranker()

    api_key = getattr(settings_obj, "rerank_api_key", "") or ""
    base_url = getattr(settings_obj, "rerank_base_url", "") or ""
    model = getattr(settings_obj, "rerank_model", "") or ""
    if not api_key or not base_url or not model:
        logger.warning(
            "RERANK_ENABLED=1 但缺少 RERANK_API_KEY / RERANK_BASE_URL / RERANK_MODEL，"
            "退回 NoopReranker（保留 RRF 顺序）"
        )
        return NoopReranker()

    return HttpRerankProvider(
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_s=float(getattr(settings_obj, "rerank_timeout_s", 10.0)),
    )


__all__ = [
    "DeterministicLexicalReranker",
    "HttpRerankProvider",
    "NoopReranker",
    "Reranker",
    "build_reranker",
]
