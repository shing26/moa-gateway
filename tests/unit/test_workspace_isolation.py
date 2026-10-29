from __future__ import annotations

from app.orchestration.workspace_isolation import (
    collect_workspace_write_set,
    create_isolated_workspace,
    merge_workspace_write_sets,
)


def _writer(workspace, writer_id: str, files: dict[str, str]) -> dict:
    for relative, content in files.items():
        target = workspace.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return collect_workspace_write_set(
        source=workspace.source,
        isolated_root=workspace.root,
        writer_id=writer_id,
        task=writer_id,
        round_index=0,
    )


def test_independent_write_sets_merge_and_same_path_conflict_is_left_untouched(
    tmp_path,
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "shared.txt").write_text("base\n", encoding="utf-8")
    first = create_isolated_workspace(base)
    second = create_isolated_workspace(base)

    try:
        left = _writer(
            first,
            "round-0/subtask-0",
            {"shared.txt": "left\n", "left-only.txt": "left\n"},
        )
        right = _writer(
            second,
            "round-0/subtask-1",
            {"shared.txt": "right\n", "right-only.txt": "right\n"},
        )
    finally:
        first.cleanup()
        second.cleanup()

    merged = merge_workspace_write_sets(base, [left, right])

    assert (base / "shared.txt").read_text(encoding="utf-8") == "base\n"
    assert (base / "left-only.txt").read_text(encoding="utf-8") == "left\n"
    assert (base / "right-only.txt").read_text(encoding="utf-8") == "right\n"
    assert [item["path"] for item in merged.applied] == [
        "left-only.txt",
        "right-only.txt",
    ]
    assert len(merged.conflicts) == 1
    conflict = merged.conflicts[0]
    assert conflict["path"] == "shared.txt"
    assert conflict["reason"] == "concurrent_divergent_write"
    assert [item["writer"] for item in conflict["candidates"]] == [
        "round-0/subtask-0",
        "round-0/subtask-1",
    ]


def test_identical_parallel_writes_are_idempotent_not_conflicts(tmp_path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    first = create_isolated_workspace(base)
    second = create_isolated_workspace(base)

    try:
        left = _writer(first, "writer-a", {"same.txt": "same\n"})
        right = _writer(second, "writer-b", {"same.txt": "same\n"})
    finally:
        first.cleanup()
        second.cleanup()

    merged = merge_workspace_write_sets(base, [left, right])

    assert (base / "same.txt").read_text(encoding="utf-8") == "same\n"
    assert merged.conflicts == ()
    assert merged.applied[0]["writers"] == ["writer-a", "writer-b"]


def test_stale_base_write_is_a_conflict_not_a_last_write_win(tmp_path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    source_file = base / "file.txt"
    source_file.write_text("base\n", encoding="utf-8")
    isolated = create_isolated_workspace(base)
    try:
        write_set = _writer(isolated, "writer-a", {"file.txt": "candidate\n"})
    finally:
        isolated.cleanup()

    source_file.write_text("changed meanwhile\n", encoding="utf-8")
    merged = merge_workspace_write_sets(base, [write_set])

    assert source_file.read_text(encoding="utf-8") == "changed meanwhile\n"
    assert merged.applied == ()
    assert merged.conflicts[0]["reason"] == "stale_base"


def test_settled_write_ids_are_not_applied_twice(tmp_path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    isolated = create_isolated_workspace(base)
    try:
        write_set = _writer(isolated, "writer-a", {"file.txt": "one\n"})
    finally:
        isolated.cleanup()

    first = merge_workspace_write_sets(base, [write_set])
    second = merge_workspace_write_sets(
        base,
        [write_set],
        settled_write_ids=first.settled_write_ids,
    )

    assert len(first.applied) == 1
    assert second.applied == ()
    assert second.conflicts == ()
