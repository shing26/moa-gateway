"""仓库档案（ADR-041）：按仓库承载交付所需的配置。

`delivery run` / `delivery preflight` 过去只认全局环境变量
（``CHANGE_AGENT_VERIFY_COMMAND`` / ``CHANGE_AGENT_BASE_BRANCH``）：换一个仓库
就要改 ``.env`` 并重启进程。仓库档案把这两个字段搬到按仓库归位的 TOML：

    config/repos/<owner>/<repo>.toml

本模块是**纯函数**：只读文件系统，不读环境变量、不发网络。环境变量由调用方
传入（``resolve_delivery_config``），这样优先级与失败姿态都能在单测里直接钉住。

失败姿态是 fail-closed：档案**存在但损坏**时抛 ``RepoProfileError``，调用方
必须报错退出，**绝不**回退环境变量。悄悄回退等于拿另一个仓库的验证命令去验
一个它不认识的工作区——那是最危险的静默错误。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: owner/name 各段允许的字符。GitHub 的仓库名实际更严格，这里取保守超集；
#: 关键是拒绝分隔符、``..`` 与空白，别让 ``--repo`` 变成路径穿越。
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

#: 档案缺省的分支名，与 `app/config.py` 的 ``CHANGE_AGENT_BASE_BRANCH`` 默认一致。
DEFAULT_BASE_BRANCH = "main"


class RepoProfileError(Exception):
    """仓库档案无法定位、越界或解析失败。消息里必须带文件路径与原因。"""


@dataclass(frozen=True)
class RepoProfile:
    """一份解析成功的仓库档案。"""

    verify_command: tuple[str, ...]
    fast_verify_command: tuple[str, ...]
    base_branch: str


@dataclass(frozen=True)
class ResolvedDeliveryConfig:
    """`delivery run` / `delivery preflight` 实际使用的配置及其来源。

    ``profile_path`` 为 ``None`` 表示没有档案，两个字段都来自环境变量；
    否则所有字段都来自档案（档案优先，不与环境变量混合）。
    """

    verify_command: tuple[str, ...]
    fast_verify_command: tuple[str, ...]
    base_branch: str
    profile_path: Path | None

    @property
    def verify_source(self) -> str:
        if self.profile_path is not None:
            return f"profile {_display_path(self.profile_path)}"
        return "env CHANGE_AGENT_VERIFY_COMMAND"

    @property
    def fast_verify_source(self) -> str:
        if self.profile_path is not None:
            return f"profile {_display_path(self.profile_path)}"
        return "derived from verify_command"

    @property
    def base_source(self) -> str:
        if self.profile_path is not None:
            return f"profile {_display_path(self.profile_path)}"
        return "env CHANGE_AGENT_BASE_BRANCH"


def _display_path(path: Path) -> str:
    """给提示行用的路径：在 cwd 之内就写相对路径，否则写绝对路径（posix 分隔符）。"""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def repo_profile_path(repo: str, root: Path | str) -> Path:
    """校验 ``repo`` 并返回档案的绝对路径（不必存在）。

    越界或非法输入抛 ``RepoProfileError``。``Path.resolve()`` 会跟随父目录与
    文件本身的符号链接，因此构造出的真实路径也必须仍落在 ``root`` 之内。
    """
    if not isinstance(repo, str) or not _REPO_PATTERN.fullmatch(repo):
        raise RepoProfileError(
            f"不是合法的仓库名 {repo!r}：必须是 owner/name，"
            "两段各自只允许 [A-Za-z0-9._-]，且不得包含 '..' 或额外分隔符"
        )
    if any(part == ".." for part in repo.split("/")):
        raise RepoProfileError(f"不是合法的仓库名 {repo!r}：不允许 '..'")

    resolved_root = Path(root).resolve()
    candidate = (resolved_root / f"{repo}.toml").resolve()
    if not candidate.is_relative_to(resolved_root):
        raise RepoProfileError(
            f"仓库档案路径越界：{candidate} 不在档案根 {resolved_root} 之内"
        )
    return candidate


def _load_repo_profile(repo: str, root: Path | str) -> tuple[RepoProfile | None, Path]:
    path = repo_profile_path(repo, root)
    if not path.exists():
        return None, path
    if not path.is_file():
        raise RepoProfileError(f"仓库档案不是普通文件: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RepoProfileError(f"无法读取仓库档案 {path}: {exc}") from exc

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RepoProfileError(f"仓库档案不是合法 TOML: {path}: {exc}") from exc

    raw_verify = data.get("verify_command")
    if raw_verify is None:
        raise RepoProfileError(f"仓库档案缺少必填字段 verify_command: {path}")
    if not isinstance(raw_verify, str):
        raise RepoProfileError(f"仓库档案 verify_command 必须是字符串: {path}")

    # 与环境变量同一套 argv 语义：直接复用既有解析函数，避免两处漂移。
    from apps.code_review_pipeline.change.pipeline import verify_command_from_env

    try:
        verify_command = verify_command_from_env(raw_verify)
    except ValueError as exc:
        raise RepoProfileError(
            f"仓库档案 verify_command 无法按 shell 语义拆分: {path}: {exc}"
        ) from exc
    if not verify_command:
        raise RepoProfileError(f"仓库档案 verify_command 不能为空: {path}")

    raw_fast = data.get("fast_verify_command")
    if raw_fast is None:
        fast_verify_command = verify_command
    else:
        if not isinstance(raw_fast, str):
            raise RepoProfileError(
                f"仓库档案 fast_verify_command 必须是字符串: {path}"
            )
        try:
            fast_verify_command = verify_command_from_env(raw_fast)
        except ValueError as exc:
            raise RepoProfileError(
                f"仓库档案 fast_verify_command 无法按 shell 语义拆分: {path}: {exc}"
            ) from exc
        if not fast_verify_command:
            raise RepoProfileError(
                f"仓库档案 fast_verify_command 不能为空: {path}"
            )

    raw_base = data.get("base_branch", DEFAULT_BASE_BRANCH)
    if not isinstance(raw_base, str) or not raw_base.strip():
        raise RepoProfileError(f"仓库档案 base_branch 必须是非空字符串: {path}")

    return (
        RepoProfile(
            verify_command=verify_command,
            fast_verify_command=fast_verify_command,
            base_branch=raw_base.strip(),
        ),
        path,
    )


def load_repo_profile(repo: str, root: Path | str) -> RepoProfile | None:
    """读取并校验 ``root/<owner>/<name>.toml``。

    档案不存在返回 ``None``（调用方回退环境变量）；存在但损坏抛
    ``RepoProfileError``——**不回退**。不读环境变量、不发网络。
    """
    profile, _ = _load_repo_profile(repo, root)
    return profile


def resolve_delivery_config(
    repo: str,
    root: Path | str,
    *,
    env_verify_command: str = "",
    env_base_branch: str = DEFAULT_BASE_BRANCH,
) -> ResolvedDeliveryConfig:
    """按契约优先级解析交付配置。

    - 档案存在 → 全部字段来自档案（即使环境变量另有一套）；
    - 档案不存在 → ``env_verify_command`` 按既有 shlex 语义解析，
      ``env_base_branch`` 作为分支；
    - 档案存在但损坏 → ``RepoProfileError``，调用方必须退出，不得回退环境变量。

    环境变量只是入参，本函数不读 ``os.environ``。
    """
    profile, path = _load_repo_profile(repo, root)
    if profile is not None:
        return ResolvedDeliveryConfig(
            verify_command=profile.verify_command,
            fast_verify_command=profile.fast_verify_command,
            base_branch=profile.base_branch,
            profile_path=path,
        )

    from apps.code_review_pipeline.change.pipeline import verify_command_from_env

    verify_command = verify_command_from_env(env_verify_command)
    return ResolvedDeliveryConfig(
        verify_command=verify_command,
        fast_verify_command=verify_command,
        base_branch=(env_base_branch or "").strip() or DEFAULT_BASE_BRANCH,
        profile_path=None,
    )


__all__ = [
    "DEFAULT_BASE_BRANCH",
    "RepoProfile",
    "RepoProfileError",
    "ResolvedDeliveryConfig",
    "load_repo_profile",
    "repo_profile_path",
    "resolve_delivery_config",
]
