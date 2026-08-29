"""BD-01 keyword scoring, shared by every vector store backend.

Why this module exists
----------------------
The gateway must stay useful when no embedding provider is configured. That
degraded path is not a stub: it is real retrieval, just lexical instead of
semantic. It used to live as private methods on the in-memory
``VectorDBClient``. Moving it here means the PostgreSQL backend scores
candidates with *exactly* the same function instead of growing a second
implementation that silently drifts into different ranking behaviour.

The scoring is CJK-aware: Chinese has no whitespace word boundaries, so the
tokeniser emits character bigrams (and drops a small stop-pair list) instead
of trying to split on spaces that never appear.
"""

from __future__ import annotations

CJK_STOP = frozenset("的了吗呢是在与和或及等有一不")

CJK_RANGES = (
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3400, 0x4DBF),  # CJK Extension A
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x2A6DF),  # CJK Extension B
)

# ASCII tokens are usually whole words ("redis", "timeout") and therefore carry
# more signal per hit than a CJK bigram, which is only half a word.
ASCII_TOKEN_WEIGHT = 1.0
CJK_TOKEN_WEIGHT = 0.6


def is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in CJK_RANGES)


def cjk_bigrams(chars: list[str]) -> list[str]:
    bigrams: list[str] = []
    for i in range(len(chars) - 1):
        pair = chars[i] + chars[i + 1]
        if pair[0] in CJK_STOP or pair[1] in CJK_STOP:
            continue
        bigrams.append(pair)
    return bigrams


def query_tokens(query_lower: str) -> list[str]:
    """Split a lowercased query into ASCII word tokens and CJK bigrams."""
    tokens: list[str] = []
    ascii_buf: list[str] = []
    cjk_buf: list[str] = []
    for ch in query_lower:
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            if cjk_buf:
                tokens.extend(cjk_bigrams(cjk_buf))
                cjk_buf = []
            ascii_buf.append(ch)
        elif is_cjk(ch):
            if ascii_buf:
                tokens.append("".join(ascii_buf))
                ascii_buf = []
            cjk_buf.append(ch)
        else:
            if ascii_buf:
                tokens.append("".join(ascii_buf))
                ascii_buf = []
            if cjk_buf:
                tokens.extend(cjk_bigrams(cjk_buf))
                cjk_buf = []
    if ascii_buf:
        tokens.append("".join(ascii_buf))
    if cjk_buf:
        tokens.extend(cjk_bigrams(cjk_buf))
    return tokens


def keyword_score(content: str, query_lower: str) -> float:
    """Raw lexical score: occurrence count weighted by token type."""
    content_lower = content.lower()
    score = 0.0
    for word in query_tokens(query_lower):
        count = content_lower.count(word)
        if not count:
            continue
        score += count * (ASCII_TOKEN_WEIGHT if word.isascii() else CJK_TOKEN_WEIGHT)
    return score


def normalized_keyword_score(content: str, query_lower: str, pool_best: float) -> float:
    """Keyword signal scaled to 0..1 so it can be fused with cosine similarity.

    ``pool_best`` is the highest raw score across the candidate pool. Min-max
    normalising within the pool (rather than using an absolute constant) keeps
    the keyword term comparable to a vector score no matter how long the query
    or the documents happen to be.
    """
    if pool_best <= 0:
        return 0.0
    return min(keyword_score(content, query_lower) / pool_best, 1.0)


def match_metadata(doc_meta: dict, filter_meta: dict) -> bool:
    """AND-equality match.

    Deliberately mirrors the PostgreSQL ``metadata @> %s::jsonb`` operator so
    both backends filter identically.
    """
    return all(doc_meta.get(k) == v for k, v in filter_meta.items())


__all__ = [
    "ASCII_TOKEN_WEIGHT",
    "CJK_TOKEN_WEIGHT",
    "cjk_bigrams",
    "is_cjk",
    "keyword_score",
    "match_metadata",
    "normalized_keyword_score",
    "query_tokens",
]
