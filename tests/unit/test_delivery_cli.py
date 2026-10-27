from __future__ import annotations

from app.cli import build_parser


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
