"""Environment policy for repository code executed by an Agent."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# Only process/toolchain variables cross the verification boundary. Unknown
# variables are dropped by default so new credential names cannot leak merely
# because nobody remembered to add them to a denylist.
_ALLOWED_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONDONTWRITEBYTECODE",
        "CI",
        "GITHUB_ACTIONS",
        "GITHUB_WORKSPACE",
        "GITHUB_SHA",
        "GITHUB_REF",
        "GITHUB_HEAD_REF",
        "GITHUB_BASE_REF",
        "RUNNER_OS",
        "RUNNER_ARCH",
        "RUNNER_TEMP",
        "RUNNER_TOOL_CACHE",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "PIP_CACHE_DIR",
    }
)
_GIT_OVERRIDES = {
    "GIT_TERMINAL_PROMPT": "0",
}


def isolated_env(
    workspace: Path,
    *,
    base: Mapping[str, str] | None = None,
    git: bool = False,
) -> dict[str, str]:
    """Build a child-process environment from an explicit allowlist."""
    source = os.environ if base is None else base
    env = {
        name.upper() if os.name == "nt" else name: value
        for name, value in source.items()
        if name.upper() in _ALLOWED_NAMES
    }

    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{workspace}{os.pathsep}{existing}" if existing else str(workspace)
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if git:
        env.update(_GIT_OVERRIDES)
    return env


def harden_git_argv(args: list[str]) -> list[str]:
    """Force repository-independent, non-extensible Git command-line options."""
    return [
        "git",
        # Worktree-local config is agent-writable, so these disable executable hooks.
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.fsmonitor=false",
        *args,
    ]


__all__ = ["harden_git_argv", "isolated_env"]
