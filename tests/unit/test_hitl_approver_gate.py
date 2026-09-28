"""审批人闸门与审批链路可见性（2026-09-28）。

修的洞：判定此前写成 ``if allowlist and operator not in allowlist``——白名单为空等于
**门开着**，任何能看到卡片的人点一下都能批准。于是"审批"不具备任何权威性，
"谁批的"也答不出来；而签名 token 那道门早就是 fail-closed，同一个模块里两种姿势。

现在三态：配了白名单 → 只认白名单；没配但显式 ``GATEWAY_ALLOW_INSECURE=1`` → 放行
（本地/演示的逃生口）；没配也没显式 → **拒**。
"""

from __future__ import annotations

from app.middleware.auth import approver_gate_error


class TestApproverGate:
    def test_empty_allowlist_without_insecure_is_refused(self):
        """**这条就是修的洞**：没配白名单、又没显式 insecure，一律拒。"""
        assert approver_gate_error("ou_x", (), raw_insecure="") is not None

    def test_empty_allowlist_with_explicit_insecure_is_allowed(self):
        """显式 insecure 时才放行——与签名 token 的逃生口是同一个开关。"""
        assert approver_gate_error("ou_x", (), raw_insecure="1") is None
        assert approver_gate_error("", (), raw_insecure="yes") is None

    def test_allowlist_lets_the_insider_through(self):
        assert approver_gate_error("ou_a", ("ou_a",), raw_insecure="") is None

    def test_allowlist_blocks_the_outsider_even_when_insecure(self):
        """配了白名单就以白名单为准——insecure 不给它开后门。"""
        assert approver_gate_error("ou_b", ("ou_a",), raw_insecure="1") is not None

    def test_missing_operator_is_refused_when_allowlist_is_set(self):
        """取不到点击者（平台没带 operator）时必须拒：验不了就不放行。"""
        assert approver_gate_error("", ("ou_a",), raw_insecure="1") is not None

    def test_rejection_reason_names_the_missing_config(self):
        """拒绝原因要能照着修（进日志），不能只说"没权限"。"""
        reason = approver_gate_error("", (), raw_insecure="")
        assert reason is not None
        assert "HITL_APPROVER_IDS" in reason
        assert "GATEWAY_ALLOW_INSECURE" in reason
