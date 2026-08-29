#!/usr/bin/env python3
"""CI guard — forbid scripts/ from patching app/ source (P3 anti-pattern backstop).

Part of delivery §6 P3: stop the "string-rewrite scripts" pattern
(gen_main / patch_main / fix_main_all / fix_name / fix_otel / update_feishu_init /
add_healthz / add_intents / add_privacy_api ...) from re-appearing. Each of those
called str.replace() on a string literal that looked like app source (imports,
route decorators, function defs, or a path under app/), mutating app/main.py and
friends in place with no type checking — the direct cause of the A/B wiring
breakage (root cause E).

A script directly under scripts/ is rejected when it calls str.replace() whose
old-string contains an app-source hint. (Subdirectories such as scripts/redteam/
are out of scope for this guard and are not scanned.)

Usage:  uv run python scripts/check_no_source_patch.py
Exit 0 = clean, 1 = offender found (printed to stdout).
"""
from __future__ import annotations

import ast
import pathlib
import sys

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
SELF_NAME = pathlib.Path(__file__).resolve().name

APP_SOURCE_HINTS = (
    "from app.",
    "import app.",
    "@app.",
    "@router.",
    "async def ",
    "= intent if agent",
    "app/main.py",
    "app/router",
    "app/routes",
    "app/channels",
    "app/agents",
    "app/deps",
    "app/pipeline",
)


def _const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _scan_file(path: pathlib.Path) -> list[str]:
    problems: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [f"{path.name}:{exc.lineno}: syntax error: {exc.msg}"]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            and node.args
        ):
            old = _const(node.args[0])
            if old:
                for hint in APP_SOURCE_HINTS:
                    if hint in old:
                        problems.append(
                            f"{path.name}:{node.lineno}: str.replace() patches app source (hint '{hint}')"
                        )
                        break
    return problems


def main() -> int:
    problems: list[str] = []
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        if path.name == SELF_NAME:
            continue
        problems.extend(_scan_file(path))
    if problems:
        print("FAIL: scripts/ contains the source-patch anti-pattern:")
        for p in problems:
            print("  - " + p)
        print(
            "\nThese scripts must not rewrite app/ source. Harden the wiring into "
            "the canonical module instead (delivery §6 P3)."
        )
        return 1
    print("OK: no source-patch scripts in scripts/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
