"""仓库档案（ADR-041）的验收用例。

对应冻结契约 ``plans/repo-profile-20261117.md`` 的 criteria 1–6：

1. 纯函数解析与校验（不读 env、不发网络），错误带路径与原因；
2. 优先级三条路：档案 > 环境变量 > 既有报错；
3. 坏档案 fail-closed，且在「环境变量有效」时也不回退；
4. `repo` 形态与路径越界必须被拒绝；
5. `delivery run` / `preflight` 打印来源；
6. 没有档案时保持既有语义（同一套 shlex、同一句报错、同一个 exit code）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import (
    _apply_delivery_defaults,
    build_parser,
    cmd_delivery_preflight,
    cmd_delivery_run,
)
from apps.code_review_pipeline.delivery.repo_profile import (
    DEFAULT_BASE_BRANCH,
    RepoProfile,
    RepoProfileError,
    load_repo_profile,
    repo_profile_path,
    resolve_delivery_config,
)


def _write_profile(
    root: Path,
    repo: str = "owner/name",
    body: str = 'verify_command = "python -m pytest -q"\n',
) -> Path:
    path = root / f"{repo}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _delivery_run_args(repo: str = "owner/name"):
    return build_parser().parse_args(
        [
            "delivery",
            "run",
            "--repo",
            repo,
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


def _preflight_args(*extra: str):
    return build_parser().parse_args(
        ["delivery", "preflight", "--repo", "owner/name", *extra]
    )


class _FakeGitHub:
    async def aclose(self) -> None:
        return None


class _FakeMergeStore:
    async def close(self) -> None:
        return None


class _FakePreflightGitHub:
    """``run_preflight`` 只读路径所需的最小假 GitHub。"""

    async def get_authenticated_login(self) -> str:
        return "shing26"

    async def get_repo(self, target) -> dict:
        return {"permission": "admin", "permissions": {"push": True}}

    async def get_branch_sha(self, target, base_branch) -> str:
        return "a" * 40

    async def aclose(self) -> None:
        return None


class _FakePreflightMergeStore:
    def list_open(self):
        return []

    async def close(self) -> None:
        return None


def _install_preflight_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings
    from apps.code_review_pipeline import merge_store as merge_store_module
    from apps.code_review_pipeline.routing import github_provider

    monkeypatch.setattr(settings, "delivery_repo_profile_dir", str(tmp_path / "repos"))
    monkeypatch.setattr(settings, "change_agent_verify_command", "")
    monkeypatch.setattr(settings, "change_agent_base_branch", "")
    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(
        github_provider, "build_github_client", lambda: _FakePreflightGitHub()
    )
    monkeypatch.setattr(
        merge_store_module, "PostgresMergeStore", _FakePreflightMergeStore
    )
    monkeypatch.setattr(
        merge_store_module, "build_merge_store", lambda: _FakePreflightMergeStore()
    )


# ── criterion 1：纯函数解析与校验 ─────────────────────────────────────────


def test_load_repo_profile_parses_fields(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        body='verify_command = "python -m pytest -q"\nbase_branch = "trunk"\n',
    )

    profile = load_repo_profile("owner/name", tmp_path)

    assert profile == RepoProfile(
        verify_command=("python", "-m", "pytest", "-q"),
        fast_verify_command=("python", "-m", "pytest", "-q"),
        base_branch="trunk",
    )


def test_base_branch_defaults_to_main(tmp_path: Path) -> None:
    _write_profile(tmp_path)

    profile = load_repo_profile("owner/name", tmp_path)

    assert profile is not None
    assert profile.base_branch == DEFAULT_BASE_BRANCH == "main"


def test_fast_verify_falls_back_to_verify_command(tmp_path: Path) -> None:
    _write_profile(
        tmp_path, body='verify_command = "python -m pytest tests/unit -q"\n'
    )

    profile = load_repo_profile("owner/name", tmp_path)

    assert profile is not None
    assert profile.fast_verify_command == profile.verify_command == (
        "python",
        "-m",
        "pytest",
        "tests/unit",
        "-q",
    )


def test_fast_verify_command_is_parsed_with_the_same_shlex_semantics(
    tmp_path: Path,
) -> None:
    _write_profile(
        tmp_path,
        body=(
            'verify_command = "python -m pytest tests/unit -q"\n'
            'fast_verify_command = "python -m pytest -q"\n'
        ),
    )

    profile = load_repo_profile("owner/name", tmp_path)

    assert profile is not None
    assert profile.verify_command == (
        "python",
        "-m",
        "pytest",
        "tests/unit",
        "-q",
    )
    assert profile.fast_verify_command == ("python", "-m", "pytest", "-q")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            (
                'verify_command = "python -m pytest -q"\n'
                'fast_verify_command = "   "\n'
            ),
            "fast_verify_command 不能为空",
        ),
        (
            (
                'verify_command = "python -m pytest -q"\n'
                "fast_verify_command = [1]\n"
            ),
            "fast_verify_command 必须是字符串",
        ),
    ],
)
def test_corrupt_fast_verify_fails_closed(
    tmp_path: Path, body: str, reason: str
) -> None:
    _write_profile(tmp_path, body=body)

    with pytest.raises(RepoProfileError) as exc:
        load_repo_profile("owner/name", tmp_path)

    assert reason in str(exc.value)


def test_missing_profile_returns_none(tmp_path: Path) -> None:
    assert load_repo_profile("owner/name", tmp_path) is None


def test_verify_command_uses_the_same_shlex_semantics_as_env(tmp_path: Path) -> None:
    """带引号的参数必须与既有环境变量解析出完全相同的 argv。"""
    from apps.code_review_pipeline.change.pipeline import verify_command_from_env

    raw = 'python -m pytest "tests/unit/test x.py" -q'
    _write_profile(tmp_path, body=f"verify_command = {raw!r}\n")

    profile = load_repo_profile("owner/name", tmp_path)

    assert profile is not None
    assert profile.verify_command == verify_command_from_env(raw)
    assert profile.verify_command[3] == "tests/unit/test x.py"


def test_load_repo_profile_does_not_read_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 环境变量里放一套完全不同的值；纯函数必须无视它。
    monkeypatch.setenv("CHANGE_AGENT_VERIFY_COMMAND", "env-only-command")
    monkeypatch.setenv("CHANGE_AGENT_BASE_BRANCH", "env-only-branch")
    _write_profile(tmp_path, body='verify_command = "profile-command"\n')

    profile = load_repo_profile("owner/name", tmp_path)

    assert profile is not None
    assert profile.verify_command == ("profile-command",)
    assert profile.base_branch == "main"


def test_resolve_without_profile_ignores_environment_unless_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHANGE_AGENT_VERIFY_COMMAND", "should-not-be-used")

    resolved = resolve_delivery_config("owner/name", tmp_path)

    assert resolved.verify_command == ()
    assert resolved.base_branch == "main"
    assert resolved.profile_path is None


def test_error_message_carries_path_and_reason(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, body="verify_command = [unclosed\n")

    with pytest.raises(RepoProfileError) as exc:
        load_repo_profile("owner/name", tmp_path)

    message = str(exc.value)
    assert str(path) in message
    assert "TOML" in message


# ── criterion 2：优先级三条路 ─────────────────────────────────────────────


def test_profile_wins_over_environment(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        body='verify_command = "profile-command"\nbase_branch = "trunk"\n',
    )

    resolved = resolve_delivery_config(
        "owner/name",
        tmp_path,
        env_verify_command="env-command --flag",
        env_base_branch="env-branch",
    )

    assert resolved.verify_command == ("profile-command",)
    assert resolved.base_branch == "trunk"
    assert resolved.profile_path is not None
    assert resolved.verify_source.startswith("profile ")
    assert resolved.fast_verify_command == resolved.verify_command
    assert resolved.base_source.startswith("profile ")


def test_profile_default_base_branch_wins_over_env(tmp_path: Path) -> None:
    _write_profile(tmp_path)

    resolved = resolve_delivery_config(
        "owner/name",
        tmp_path,
        env_verify_command="env-command",
        env_base_branch="env-branch",
    )

    assert resolved.base_branch == "main"


def test_missing_profile_falls_back_to_environment(tmp_path: Path) -> None:
    resolved = resolve_delivery_config(
        "owner/name",
        tmp_path,
        env_verify_command='python -m pytest "tests/unit/test x.py"',
        env_base_branch="release/line",
    )

    assert resolved.verify_command == (
        "python",
        "-m",
        "pytest",
        "tests/unit/test x.py",
    )
    assert resolved.base_branch == "release/line"
    assert resolved.profile_path is None
    assert resolved.verify_source == "env CHANGE_AGENT_VERIFY_COMMAND"
    assert resolved.fast_verify_command == resolved.verify_command
    assert resolved.fast_verify_source == "derived from verify_command"
    assert resolved.base_source == "env CHANGE_AGENT_BASE_BRANCH"


def test_no_profile_and_no_env_yields_empty_verify_command(tmp_path: Path) -> None:
    resolved = resolve_delivery_config("owner/name", tmp_path, env_verify_command="")

    assert resolved.verify_command == ()


def test_delivery_run_keeps_the_existing_error_when_nothing_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "change_agent_push_enabled", True)
    monkeypatch.setattr(settings, "delivery_topology", "single")
    monkeypatch.setattr(settings, "change_agent_model", "test-model")
    monkeypatch.setattr(settings, "change_agent_verify_command", "")
    monkeypatch.setattr(settings, "delivery_repo_profile_dir", str(tmp_path / "repos"))
    args = _delivery_run_args()
    _apply_delivery_defaults(args)

    assert cmd_delivery_run(args) == 2
    captured = capsys.readouterr()
    # 与今天逐字一致（criterion 6）。
    assert captured.err.strip() == "CHANGE_AGENT_VERIFY_COMMAND 未配置"


# ── criterion 3：坏档案 fail-closed（不得回退环境变量）────────────────────


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("verify_command = [unclosed\n", "TOML"),
        ('base_branch = "main"\n', "缺少必填字段 verify_command"),
        ('verify_command = "   "\n', "verify_command 不能为空"),
    ],
)
def test_corrupt_profile_fails_closed_without_reading_env(
    tmp_path: Path, body: str, reason: str
) -> None:
    path = _write_profile(tmp_path, body=body)

    with pytest.raises(RepoProfileError) as exc:
        resolve_delivery_config(
            "owner/name",
            tmp_path,
            env_verify_command="valid-env-command",
            env_base_branch="valid-env-branch",
        )

    message = str(exc.value)
    assert str(path) in message
    assert reason in message


def test_corrupt_profile_rejects_non_string_verify_command(tmp_path: Path) -> None:
    _write_profile(tmp_path, body="verify_command = [1, 2]\n")

    with pytest.raises(RepoProfileError, match="必须是字符串"):
        load_repo_profile("owner/name", tmp_path)


def test_corrupt_profile_rejects_empty_base_branch(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        body='verify_command = "python -m pytest -q"\nbase_branch = "   "\n',
    )

    with pytest.raises(RepoProfileError, match="base_branch"):
        load_repo_profile("owner/name", tmp_path)


def test_delivery_run_exits_2_on_corrupt_profile_even_with_valid_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    profile_root = tmp_path / "repos"
    path = _write_profile(profile_root, body="verify_command = [unclosed\n")
    monkeypatch.setattr(settings, "change_agent_push_enabled", True)
    monkeypatch.setattr(settings, "delivery_topology", "single")
    monkeypatch.setattr(settings, "change_agent_model", "test-model")
    monkeypatch.setattr(settings, "change_agent_verify_command", "valid-env-command")
    monkeypatch.setattr(settings, "delivery_repo_profile_dir", str(profile_root))
    args = _delivery_run_args()
    _apply_delivery_defaults(args)

    assert cmd_delivery_run(args) == 2
    captured = capsys.readouterr()
    assert str(path) in captured.err
    # 没有打印 env 来源：坏档案没有回退到环境变量。
    assert "source: env CHANGE_AGENT_VERIFY_COMMAND" not in captured.out


# ── criterion 4：repo 形态与路径越界 ─────────────────────────────────────


@pytest.mark.parametrize(
    "repo",
    [
        "owner",  # 缺 name
        "owner/name/extra",  # 额外分隔符
        "../etc/passwd",
        "owner/../../x",
        "owner/..",
        "..",
        "owner/name with space",
        "",
    ],
)
def test_repo_profile_path_rejects_illegal_repo(tmp_path: Path, repo: str) -> None:
    with pytest.raises(RepoProfileError):
        repo_profile_path(repo, tmp_path)


def test_repo_profile_path_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "owner").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # Windows 上可能没有符号链接权限
        pytest.skip(f"本机无法创建目录符号链接: {exc}")
    (outside / "name.toml").write_text(
        'verify_command = "outside"\n', encoding="utf-8"
    )

    with pytest.raises(RepoProfileError, match="越界"):
        repo_profile_path("owner/name", root)


def test_repo_profile_path_stays_inside_root(tmp_path: Path) -> None:
    path = repo_profile_path("owner/name", tmp_path)

    assert path == (tmp_path / "owner" / "name.toml").resolve()


# ── criterion 5：来源可观测 ──────────────────────────────────────────────


def test_delivery_run_prints_profile_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    profile_root = tmp_path / "repos"
    _write_profile(
        profile_root,
        body='verify_command = "python -m pytest -q"\nbase_branch = "main"\n',
    )
    monkeypatch.setattr(settings, "change_agent_push_enabled", True)
    monkeypatch.setattr(settings, "delivery_topology", "single")
    monkeypatch.setattr(settings, "change_agent_model", "test-model")
    monkeypatch.setattr(settings, "change_agent_verify_command", "env-command")
    monkeypatch.setattr(settings, "delivery_repo_profile_dir", str(profile_root))
    monkeypatch.setattr(settings, "github_token", "")  # 停在下一道校验，不跑运行时
    args = _delivery_run_args()
    _apply_delivery_defaults(args)

    assert cmd_delivery_run(args) == 2
    captured = capsys.readouterr()
    assert "verify command: python -m pytest -q (source: profile " in captured.out
    assert "fast verify  : python -m pytest -q (source: profile " in captured.out
    assert "name.toml" in captured.out
    # 档案优先：环境变量那套没有被使用。
    assert "env-command" not in captured.out
    assert "GITHUB_TOKEN 未配置" in captured.err


def test_delivery_run_prints_env_source_without_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "change_agent_push_enabled", True)
    monkeypatch.setattr(settings, "delivery_topology", "single")
    monkeypatch.setattr(settings, "change_agent_model", "test-model")
    monkeypatch.setattr(settings, "change_agent_verify_command", "env-command --flag")
    monkeypatch.setattr(settings, "delivery_repo_profile_dir", str(tmp_path / "repos"))
    monkeypatch.setattr(settings, "github_token", "")
    args = _delivery_run_args()
    _apply_delivery_defaults(args)

    assert cmd_delivery_run(args) == 2
    assert (
        "verify command: env-command --flag "
        "(source: env CHANGE_AGENT_VERIFY_COMMAND)"
    ) in capsys.readouterr().out


def test_delivery_preflight_prints_profile_base_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    profile_root = tmp_path / "repos"
    _write_profile(
        profile_root,
        body='verify_command = "python -m pytest -q"\nbase_branch = "trunk"\n',
    )
    monkeypatch.setattr(settings, "delivery_repo_profile_dir", str(profile_root))
    monkeypatch.setattr(settings, "github_token", "")  # 停在下一道校验
    args = _preflight_args()
    _apply_delivery_defaults(args)

    assert cmd_delivery_preflight(args) == 2
    captured = capsys.readouterr()
    assert "base branch : trunk (source: profile " in captured.out
    assert "GITHUB_TOKEN 未配置" in captured.err


def test_delivery_preflight_explicit_base_branch_beats_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    profile_root = tmp_path / "repos"
    _write_profile(
        profile_root,
        body='verify_command = "python -m pytest -q"\nbase_branch = "trunk"\n',
    )
    monkeypatch.setattr(settings, "delivery_repo_profile_dir", str(profile_root))
    monkeypatch.setattr(settings, "github_token", "")
    args = _preflight_args("--base-branch", "release/2.0")
    _apply_delivery_defaults(args)

    assert cmd_delivery_preflight(args) == 2
    assert (
        "base branch : release/2.0 (source: argument --base-branch)"
        in capsys.readouterr().out
    )


def test_delivery_preflight_passes_profile_base_branch_to_the_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from apps.code_review_pipeline.delivery import preflight as preflight_module
    from apps.code_review_pipeline import merge_store as merge_store_module
    from apps.code_review_pipeline.routing import github_provider

    profile_root = tmp_path / "repos"
    _write_profile(
        profile_root,
        body='verify_command = "python -m pytest -q"\nbase_branch = "trunk"\n',
    )
    captured: dict[str, str] = {}

    async def fake_run_preflight(
        repo, *, github, base_branch, merge_store, probe_workflows, verify_command=None
    ):
        captured["base_branch"] = base_branch
        return SimpleNamespace(ok=True, checks=[])

    monkeypatch.setattr(settings, "delivery_repo_profile_dir", str(profile_root))
    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(
        github_provider, "build_github_client", lambda: _FakeGitHub()
    )
    monkeypatch.setattr(
        merge_store_module, "build_merge_store", lambda: _FakeMergeStore()
    )
    monkeypatch.setattr(preflight_module, "run_preflight", fake_run_preflight)
    args = _preflight_args()
    _apply_delivery_defaults(args)

    assert cmd_delivery_preflight(args) == 0
    assert captured["base_branch"] == "trunk"


def test_delivery_preflight_fails_closed_on_corrupt_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    profile_root = tmp_path / "repos"
    path = _write_profile(profile_root, body="verify_command = [unclosed\n")
    monkeypatch.setattr(settings, "delivery_repo_profile_dir", str(profile_root))
    monkeypatch.setattr(settings, "github_token", "token")
    args = _preflight_args()
    _apply_delivery_defaults(args)

    assert cmd_delivery_preflight(args) == 2
    captured = capsys.readouterr()
    assert str(path) in captured.err
    assert captured.out == ""


def test_delivery_preflight_json_reports_the_verify_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_preflight_runtime(tmp_path, monkeypatch)
    args = _preflight_args("--json")
    _apply_delivery_defaults(args)

    assert cmd_delivery_preflight(args) == 1
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])

    verify = next(
        check for check in payload["checks"] if check["name"] == "verify"
    )
    assert verify["status"] == "blocked"
    assert "detail" in verify
    assert "未配置" in verify["detail"]
    assert payload["ok"] is False


def test_delivery_preflight_text_output_keeps_the_check_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_preflight_runtime(tmp_path, monkeypatch)
    args = _preflight_args()
    _apply_delivery_defaults(args)

    assert cmd_delivery_preflight(args) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("base branch : main")
    rows = [line.split()[1] for line in lines[1:]]
    assert rows == ["token", "push", "branch", "workflows", "merge_store", "verify"]
    assert lines[-1].startswith("blocked  verify       verify 命令未配置/为空")


# ── criterion 6：没有档案时的既有语义 ────────────────────────────────────


def test_delivery_settings_default_profile_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import Settings

    monkeypatch.delenv("DELIVERY_REPO_PROFILE_DIR", raising=False)
    assert Settings().delivery_repo_profile_dir == "config/repos"


# ── 随仓库发布的档案：验证命令不得绑定解释器路径 ──────────────────────────


def test_shipped_profile_verifies_with_a_bare_interpreter() -> None:
    """解释器必须交给 harness 解析，不能写相对或本机绝对路径。

    交付 workspace 是新克隆的归档，验证副本还会再复制一次且不含 ``.venv``。
    ``build_verify_env``（app/agents/command_runner.py）把**启动 agent 的** venv
    放到子进程 PATH 头部，``resolve_program`` 再按裸名解析。档案里写
    ``.venv/Scripts/python.exe`` 会在任何一次真实交付的验证步骤直接
    ``FileNotFoundError``——这个回归值过一次真实交付才发现，代价是整单。
    """
    repo_root = Path(__file__).resolve().parents[2]
    profile = load_repo_profile("shing26/moa-gateway", repo_root / "config" / "repos")

    assert profile.verify_command, "随仓库发布的档案必须给出验证命令"
    assert "/" not in profile.verify_command[0]
    assert "\\" not in profile.verify_command[0]
