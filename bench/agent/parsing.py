"""agent 输出里反复出现的小型结构化清洗。"""

from __future__ import annotations


def nonnegative_indices(raw: object) -> tuple[int, ...]:
    """把模型给的列表收成去重保序的非负整数下标。"""
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        value = int(item)
        if value >= 0 and value not in seen:
            seen.append(value)
    return tuple(seen)


__all__ = ["nonnegative_indices"]
