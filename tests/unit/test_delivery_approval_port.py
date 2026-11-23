from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_app_cli_reexports_the_delivery_port_objects() -> None:
    from app.cli import TerminalPushApprovalPort as cli_port
    from app.cli import _build_push_approval_port as cli_factory
    from apps.code_review_pipeline.delivery import approval_port

    assert cli_port is approval_port.TerminalPushApprovalPort
    assert cli_factory is approval_port.build_push_approval_port


def test_build_push_approval_port_defaults_to_terminal() -> None:
    from apps.code_review_pipeline.delivery.approval_port import (
        TerminalPushApprovalPort,
        build_push_approval_port,
    )

    cfg = SimpleNamespace(feishu_push_approval_enabled=False)

    port = build_push_approval_port(github=None, settings_obj=cfg)

    assert isinstance(port, TerminalPushApprovalPort)


def test_build_push_approval_port_uses_feishu_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from app.config import settings
    from apps.code_review_pipeline.delivery.approval_port import (
        build_push_approval_port,
    )
    from apps.code_review_pipeline.delivery.feishu_approval import (
        FeishuPushApprovalPort,
    )

    monkeypatch.setattr(settings, "feishu_push_approval_enabled", True)
    monkeypatch.setattr(settings, "feishu_app_id", "cli_test")
    monkeypatch.setattr(settings, "feishu_app_secret", "secret")
    monkeypatch.setattr(settings, "feishu_push_approval_timeout_s", 600)
    monkeypatch.setattr(settings, "delivery_run_record_dir", str(tmp_path / "runs"))

    port = build_push_approval_port(github=None)

    assert isinstance(port, FeishuPushApprovalPort)
    assert port._settings is settings
