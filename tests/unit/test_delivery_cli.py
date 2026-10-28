from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import (
    TerminalPushApprovalPort,
    _apply_delivery_defaults,
    build_parser,
    cmd_delivery_ci_triage,
    cmd_delivery_run,
)


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


def _ci_triage_argv() -> list[str]:
    return [
        "delivery",
        "ci-triage",
        "--repo",
        "owner/name",
        "--sha",
        "a" * 40,
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


def test_ci_triage_defaults_derive_trace_and_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "change_agent_base_branch", "release/line")
    args = build_parser().parse_args(_ci_triage_argv())

    _apply_delivery_defaults(args)

    assert args.base_branch == "release/line"
    assert args.branch == "moa/ci-fix-aaaaaaaaaaaa"
    assert args.trace_id == "ci-owner-name-aaaaaaaaaaaa"
    assert args.func is cmd_delivery_ci_triage


def test_cmd_delivery_run_rejects_push_disabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "change_agent_push_enabled", False)
    args = build_parser().parse_args(_delivery_argv())
    _apply_delivery_defaults(args)

    assert cmd_delivery_run(args) == 2
    assert "CHANGE_AGENT_PUSH_ENABLED=0" in capsys.readouterr().err


def test_cmd_ci_triage_requires_github_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "github_token", "")
    args = build_parser().parse_args(_ci_triage_argv())
    _apply_delivery_defaults(args)

    assert cmd_delivery_ci_triage(args) == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_cmd_delivery_run_rejects_injected_verified_topology(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "change_agent_push_enabled", True)
    monkeypatch.setattr(settings, "delivery_topology", "verified")
    args = build_parser().parse_args(_delivery_argv())
    _apply_delivery_defaults(args)

    assert cmd_delivery_run(args) == 2
    assert "verified topology" in capsys.readouterr().err


def test_terminal_approval_denies_non_tty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class NonInteractiveStdin:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("app.cli.sys.stdin", NonInteractiveStdin())
    bundle = SimpleNamespace(
        branch="moa/fix-1",
        bundle_hash="bundle-1",
        patch_hash="patch-1",
        verify_exit_code=0,
        trace_id="trace-1",
    )

    approval = asyncio.run(
        TerminalPushApprovalPort().request(run_id="run-1", bundle=bundle)
    )

    assert approval is None
    assert "non-interactive stdin" in capsys.readouterr().out


def test_terminal_approval_binds_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    class InteractiveStdin:
        def isatty(self) -> bool:
            return True

    answers = iter(["alice", "APPROVE"])
    monkeypatch.setattr("app.cli.sys.stdin", InteractiveStdin())
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    bundle = SimpleNamespace(
        branch="moa/fix-1",
        bundle_hash="bundle-1",
        patch_hash="patch-1",
        verify_exit_code=0,
        trace_id="trace-1",
    )

    approval = asyncio.run(
        TerminalPushApprovalPort().request(run_id="run-1", bundle=bundle)
    )

    assert approval is not None
    assert approval.run_id == "run-1"
    assert approval.trace_id == "trace-1"
    assert approval.bundle_hash == "bundle-1"


def test_cmd_delivery_run_wires_delivery_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.agents import provider
    from app.config import settings
    from apps.code_review_pipeline.delivery.adapters import github as github_adapter_module
    from apps.code_review_pipeline.delivery.adapters import (
        production_agent as production_agent_module,
    )
    from apps.code_review_pipeline.delivery import flow as flow_module
    from apps.code_review_pipeline.delivery import workspace as workspace_module
    from apps.code_review_pipeline import merge_store as merge_store_module
    from apps.code_review_pipeline.routing import github_provider

    captured: dict[str, object] = {}

    class FakeConfig:
        def __init__(self) -> None:
            self.model = ""

        @classmethod
        def from_env(cls, prefix: str) -> "FakeConfig":
            captured["llm_prefix"] = prefix
            return cls()

    class FakeLLM:
        def __init__(self, config) -> None:
            self.config = config
            captured["llm"] = self

        async def aclose(self) -> None:
            captured["llm_closed"] = True

    class FakeGitHub:
        async def aclose(self) -> None:
            captured["github_closed"] = True

    class FakeProvisioner:
        def __init__(self, *, github) -> None:
            captured["provisioner_github"] = github

        async def materialize(self, *, repo, base_sha, destination):
            destination.mkdir(parents=True)
            captured["workspace_call"] = (repo, base_sha, destination)
            return destination

    class FakeImplementer:
        def __init__(self, **kwargs) -> None:
            captured["implementer"] = kwargs

    class FakeAdapter:
        def __init__(self, **kwargs) -> None:
            captured["adapter"] = kwargs

    class FakeFlow:
        def __init__(self, **kwargs) -> None:
            captured["flow"] = kwargs

        async def run(self, request):
            captured["request"] = request
            return SimpleNamespace(
                run_id="run-1",
                state=SimpleNamespace(value="waiting_ci"),
                failure=None,
                push=None,
            )

    merge_store = merge_store_module.PostgresMergeStore(
        dsn="postgresql://unused",
        _psycopg=object(),
    )
    monkeypatch.setattr(provider, "LLMConfig", FakeConfig)
    monkeypatch.setattr(provider, "LLMClient", FakeLLM)
    monkeypatch.setattr(
        github_provider,
        "build_github_client",
        lambda: FakeGitHub(),
    )
    monkeypatch.setattr(
        workspace_module,
        "GitHubWorkspaceProvisioner",
        FakeProvisioner,
    )
    monkeypatch.setattr(
        flow_module,
        "DeliveryFlow",
        FakeFlow,
    )
    monkeypatch.setattr(
        production_agent_module,
        "ProductionImplementer",
        FakeImplementer,
    )
    monkeypatch.setattr(
        github_adapter_module,
        "GitHubChangeAdapter",
        FakeAdapter,
    )
    monkeypatch.setattr(
        merge_store_module,
        "build_merge_store",
        lambda: merge_store,
    )
    monkeypatch.setattr(settings, "change_agent_push_enabled", True)
    monkeypatch.setattr(settings, "change_agent_model", "test-model")
    monkeypatch.setattr(
        settings,
        "change_agent_verify_command",
        "python -m pytest -q",
    )
    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(settings, "delivery_topology", "single")
    monkeypatch.setattr(settings, "delivery_max_steps", 12)
    monkeypatch.setattr(settings, "delivery_max_tokens", 3456)
    monkeypatch.setattr(settings, "delivery_run_record_dir", str(tmp_path / "runs"))
    monkeypatch.setattr(settings, "change_agent_base_branch", "main")

    args = build_parser().parse_args(
        [
            *_delivery_argv(),
            "--trace-id",
            "trace-cli",
        ]
    )
    _apply_delivery_defaults(args)

    assert cmd_delivery_run(args) == 0
    output = capsys.readouterr().out
    assert "run_id    : run-1" in output
    request = captured["request"]
    assert request.trace_id == "trace-cli"
    assert request.repo == "owner/name"
    flow = captured["flow"]
    assert flow["policy"].max_steps == 12
    assert flow["policy"].max_tokens == 3456
    assert flow["implementer"] is not None
    assert flow["preparer"] is flow["publisher"]
    assert captured["llm"].config.model == "test-model"
    assert captured["llm_closed"] is True
    assert captured["github_closed"] is True


def test_cmd_ci_triage_collects_and_writes_only_local_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings
    from apps.code_review_pipeline.delivery.adapters import (
        github_ci as github_ci_module,
    )
    from apps.code_review_pipeline.delivery.ci_triage import (
        CiFailureCheck,
        CiFailureEvidence,
        CiJobLog,
    )
    from apps.code_review_pipeline.routing import github_provider

    captured: dict[str, object] = {}

    class FakeGitHub:
        async def aclose(self) -> None:
            captured["closed"] = True

    class FakeCollector:
        def __init__(self, *, github, max_jobs, max_log_chars) -> None:
            captured["collector"] = (github, max_jobs, max_log_chars)

        async def collect(self, *, repo, head_sha):
            captured["identity"] = (repo, head_sha)
            return CiFailureEvidence(
                repo=repo,
                head_sha=head_sha,
                checks=(
                    CiFailureCheck(
                        name="test",
                        conclusion="failure",
                        url="https://github.com/owner/name/actions/runs/1",
                    ),
                ),
                jobs=(
                    CiJobLog(
                        run_id=1,
                        job_id=2,
                        workflow_name="CI",
                        name="pytest",
                        conclusion="failure",
                        log="FAILED tests/test_api.py - AssertionError\n",
                    ),
                ),
            )

    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(settings, "ci_triage_output_dir", str(tmp_path / "ci"))
    monkeypatch.setattr(settings, "ci_triage_max_jobs", 4)
    monkeypatch.setattr(settings, "ci_triage_max_log_chars", 1234)
    monkeypatch.setattr(
        github_provider, "build_github_client", lambda: FakeGitHub()
    )
    monkeypatch.setattr(
        github_ci_module,
        "GitHubCiEvidenceCollector",
        FakeCollector,
    )
    args = build_parser().parse_args(_ci_triage_argv())
    _apply_delivery_defaults(args)

    assert cmd_delivery_ci_triage(args) == 0

    output = capsys.readouterr().out
    assert "human review required" in output
    assert "no GitHub writes performed" in output
    assert "report.json" in output or ".json" in output
    assert captured["identity"] == ("owner/name", "a" * 40)
    assert captured["closed"] is True
    assert list((tmp_path / "ci").glob("*.request.json"))


def test_cmd_ci_triage_reports_github_http_failure_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import httpx

    from app.config import settings
    from apps.code_review_pipeline.delivery.adapters import (
        github_ci as github_ci_module,
    )
    from apps.code_review_pipeline.routing import github_provider

    captured: dict[str, object] = {}

    class FakeGitHub:
        async def aclose(self) -> None:
            captured["closed"] = True

    class ForbiddenCollector:
        def __init__(self, *, github, max_jobs, max_log_chars) -> None:
            pass

        async def collect(self, *, repo, head_sha):
            request = httpx.Request("GET", "https://api.github.com/checks")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError(
                "forbidden",
                request=request,
                response=response,
            )

    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(settings, "ci_triage_output_dir", str(tmp_path / "ci"))
    monkeypatch.setattr(
        github_provider, "build_github_client", lambda: FakeGitHub()
    )
    monkeypatch.setattr(
        github_ci_module,
        "GitHubCiEvidenceCollector",
        ForbiddenCollector,
    )
    args = build_parser().parse_args(_ci_triage_argv())
    _apply_delivery_defaults(args)

    assert cmd_delivery_ci_triage(args) == 1

    captured_output = capsys.readouterr()
    assert "HTTP 403" in captured_output.err
    markdown_files = list((tmp_path / "ci").glob("*.md"))
    assert len(markdown_files) == 1
    assert "Checks: Read" in markdown_files[0].read_text(encoding="utf-8")
    assert str(markdown_files[0]) in captured_output.err
    assert "human review required" in captured_output.err.lower()
    assert not list((tmp_path / "ci").glob("*.request.json"))
    assert captured["closed"] is True
