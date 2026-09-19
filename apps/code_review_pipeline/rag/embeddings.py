from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("moa.code_review.rag")


class EmbeddingError(Exception):
    """Raised when embedding generation fails."""


@dataclass(frozen=True)
class EmbeddingResult:
    text: str
    vector: tuple[float, ...]
    source_type: str
    source_id: str
    metadata: dict[str, Any] | None = None


def embedding_dimension() -> int:
    raw = (
        os.getenv("CODE_REVIEW_EMBEDDING_DIM")
        or os.getenv("VECTOR_DB_EMBEDDING_DIM")
        or "1536"
    )
    try:
        dim = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid embedding dimension: {raw!r}") from exc
    if dim <= 0:
        raise ValueError(f"embedding dimension must be positive: {dim}")
    return dim


def _build_embedding_client() -> httpx.AsyncClient:
    api_key = os.getenv("CODE_REVIEW_EMBEDDING_API_KEY", "")
    base_url = os.getenv("CODE_REVIEW_EMBEDDING_BASE_URL", "https://api.openai.com/v1")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(30.0),
    )


def _normalize_vector(values: list[float]) -> tuple[float, ...]:
    return tuple(float(v) for v in values)


async def generate_embeddings(
    texts: list[str],
    *,
    model: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[EmbeddingResult]:
    """
    Generate embeddings for a list of texts using an OpenAI-compatible endpoint.

    Required env:
        CODE_REVIEW_EMBEDDING_API_KEY or OPENAI_API_KEY
        CODE_REVIEW_EMBEDDING_BASE_URL (default https://api.openai.com/v1)
        CODE_REVIEW_EMBEDDING_MODEL (default text-embedding-3-small)
    """
    if not texts:
        return []

    model = model or os.getenv("CODE_REVIEW_EMBEDDING_MODEL", "text-embedding-3-small")
    client = client or _build_embedding_client()

    try:
        if len(texts) == 1:
            payload = await _request_embeddings(client, model, texts[0])
            return _results_from_payload(payload, texts, model)

        try:
            payload = await _request_embeddings(client, model, texts)
        except Exception as exc:
            logger.warning("embedding batch input rejected, falling back to single inputs: %s", exc)
            results: list[EmbeddingResult] = []
            for text in texts:
                payload = await _request_embeddings(client, model, text)
                results.extend(_results_from_payload(payload, [text], model))
            return results
        return _results_from_payload(payload, texts, model)
    except Exception as exc:
        raise EmbeddingError(f"embedding request failed: {exc}") from exc


async def _request_embeddings(
    client: httpx.AsyncClient,
    model: str,
    input_payload: list[str] | str,
) -> dict[str, Any]:
    resp = await client.post(
        "/embeddings",
        json={"model": model, "input": input_payload},
    )
    resp.raise_for_status()
    return resp.json()


def _results_from_payload(
    payload: dict[str, Any],
    texts: list[str],
    model: str,
) -> list[EmbeddingResult]:
    data = payload.get("data", [])
    if not data:
        return []

    results: list[EmbeddingResult] = []
    for idx, item in enumerate(data):
        vector = item.get("embedding", [])
        if not vector:
            continue
        item_index = item.get("index")
        text_index = item_index if isinstance(item_index, int) and 0 <= item_index < len(texts) else idx
        text = texts[text_index] if text_index < len(texts) else ""
        results.append(
            EmbeddingResult(
                text=text,
                vector=_normalize_vector(vector),
                source_type="embedding",
                source_id=f"emb-{text_index}",
                metadata={"model": model},
            )
        )
    return results
