"""Embedding provider for the retrieval stack.

Design notes
------------
The provider is *optional by construction*. When no API key is configured the
gateway still retrieves — it just falls back to lexical (keyword) scoring
instead of vector similarity. That is the whole point of making the vector
path additive rather than mandatory.

Two failure behaviours worth knowing about:

1. Batch first, then per-item. Some OpenAI-compatible endpoints only accept a
   single string in ``input``. If the batch call fails for a multi-item
   request we stop batching for the process lifetime rather than retrying a
   request shape the server has already rejected.

2. Consecutive failures trip a circuit breaker. Hammering a dead (or 401-ing)
   embedding endpoint on every single query would add latency to the hot path
   for no benefit, so after ``max_failures`` we stop calling it entirely and
   run keyword-only until the process restarts.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("moa.vectordb.embeddings")

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class EmbeddingProvider:
    """OpenAI-compatible ``/embeddings`` client with graceful degradation."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 10.0,
        max_failures: int = 3,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout_s = timeout_s
        self._max_failures = max_failures
        self._failures = 0
        self._disabled = False
        self._batch_supported = True
        self._last_error: str | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        """False when unconfigured, or after the circuit breaker trips."""
        return bool(self._api_key) and not self._disabled

    @property
    def model(self) -> str:
        return self._model

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

    async def embed(self, text: str) -> list[float] | None:
        batch = await self.embed_batch([text])
        return batch[0] if batch else None

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """Return one vector per input text, or ``None`` where unavailable.

        Never raises. A caller that gets ``None`` is expected to fall back to
        keyword retrieval, so suppressing the error here is intentional and
        keeps the hot path alive when the embedding backend misbehaves.
        """
        if not texts:
            return []
        if not self.enabled:
            return [None] * len(texts)

        if self._batch_supported and len(texts) > 1:
            try:
                vectors = await self._post_batch(texts)
                self._failures = 0
                self._last_error = None
                return vectors
            except Exception as exc:  # noqa: BLE001 - provider is best-effort
                # A rejected batch shape is not a transient outage, so do not
                # count it against the circuit breaker; just stop batching.
                logger.warning("embedding 批量接口不可用，改为逐条调用: %s", exc)
                self._batch_supported = False

        return [await self._post_one(text) for text in texts]

    async def _post_batch(self, texts: list[str]) -> list[list[float] | None]:
        payload = await self._post({"model": self._model, "input": list(texts)})
        data = payload.get("data") or []
        if len(data) != len(texts):
            raise ValueError(f"embedding 返回 {len(data)} 条，期望 {len(texts)} 条")
        ordered: list[list[float] | None] = [None] * len(texts)
        for item in data:
            index = item.get("index")
            vector = item.get("embedding")
            if isinstance(index, int) and 0 <= index < len(texts) and isinstance(vector, list):
                ordered[index] = [float(x) for x in vector]
        if all(v is None for v in ordered):
            raise ValueError("embedding 响应中没有可用向量")
        return ordered

    async def _post_one(self, text: str) -> list[float] | None:
        try:
            payload = await self._post({"model": self._model, "input": text})
        except Exception as exc:  # noqa: BLE001 - provider is best-effort
            self._record_failure(exc)
            return None
        data = payload.get("data") or []
        if not data:
            self._record_failure(ValueError("embedding 响应为空"))
            return None
        vector = data[0].get("embedding")
        if not isinstance(vector, list) or not vector:
            self._record_failure(ValueError("embedding 响应缺少 embedding 字段"))
            return None
        self._failures = 0
        self._last_error = None
        return [float(x) for x in vector]

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client_get().post(f"{self._base_url}/embeddings", json=body)
        response.raise_for_status()
        return response.json()

    def _record_failure(self, exc: BaseException) -> None:
        self._failures += 1
        self._last_error = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning(
            "embedding 调用失败 (%d/%d): %s", self._failures, self._max_failures, self._last_error
        )
        if self._failures >= self._max_failures:
            self._disabled = True
            logger.error(
                "embedding 连续失败 %d 次，已熔断：后续检索全部走关键词回退，"
                "重启进程或修正配置后恢复",
                self._failures,
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_embedding_provider(settings_obj: Any) -> EmbeddingProvider | None:
    """Build a provider from settings, or ``None`` when unconfigured.

    ``None`` is a valid, expected outcome — it selects keyword-only retrieval,
    which is the BD-01 degraded path, not an error.
    """
    api_key = getattr(settings_obj, "embedding_api_key", "") or ""
    if not api_key:
        logger.info("未配置 EMBEDDING_API_KEY：检索走关键词回退（BD-01），语义检索关闭")
        return None
    return EmbeddingProvider(
        api_key=api_key,
        model=getattr(settings_obj, "embedding_model", "") or "text-embedding-3-small",
        base_url=getattr(settings_obj, "embedding_base_url", "") or DEFAULT_BASE_URL,
        timeout_s=float(getattr(settings_obj, "embedding_timeout_s", 10.0)),
    )


__all__ = ["DEFAULT_BASE_URL", "EmbeddingProvider", "build_embedding_provider"]
