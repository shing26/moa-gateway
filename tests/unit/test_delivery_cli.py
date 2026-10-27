from __future__ import annotations

import pytest

from app.cli import _apply_delivery_defaults, build_parser, cmd_delivery_run


def _delivery_argv() -> list[str]:
    return [
        "delivery",
        "run",
        "--repo",
        "owner/name",
        "--base-sha",
        "a" * 40,
        "--branch",
        "moa/fix-1",
        "--title",
        "fix: add",
        "--goal",
        "make add return a + b",
    ]


def test_delivery_run_parser_requires_repo_and_base_sha() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "delivery",
            "run",
            "--repo",
            "owner/name",
            "--base-sha",
            "a" * 40,
            "--branch",
            "moa/fix-1",
            "--title",
            "fix: add",
            "--goal",
            "make add return a + b",
        ]
    )

    assert args.group == "delivery"
    assert args.command == "run"
    assert args.repo == "owner/name"
    assert args.base_sha == "a" * 40
    assert args.origin == "manual"


@pytest.mark.parametrize(
    "missing",
    ["--repo", "--base-sha", "--branch", "--title", "--goal"],
)
def test_delivery_run_rejects_missing_required_argument(missing: str) -> None:
    parser = build_parser()
    argv = _delivery_argv()
    index = argv.index(missing)
    del argv[index : index + 2]

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)

    assert exc.value.code == 2


def test_delivery_defaults_use_settings_and_wire_run_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "change_agent_base_branch", "release/line")
    parser = build_parser()
    args = parser.parse_args(_delivery_argv())

    assert args.base_branch is None
    assert args.trace_id is None
    assert args.body == ""

    _apply_delivery_defaults(args)

    assert args.base_branch == "release/line"
    assert args.trace_id.startswith("delivery-")
    assert args.body == args.goal
    assert args.func is cmd_delivery_run
