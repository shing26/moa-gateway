"""HTML 渲染层：dashboard 页面的纯模板函数（M3 拆分，原 dashboard.py）。

本模块只产出 HTML 字符串，不注册路由、不碰 HTTP；数据一律通过参数或
从 ``app.services`` 取。会话详情需要"当前模式"这种运行时状态，由路由
handler 传入（``session_detail(sid, current_mode)``），因此渲染层不依赖
组合根 ``app.deps``。
"""

from __future__ import annotations

import html
import os
from typing import Any

from fastapi.responses import HTMLResponse

from app.agents.provider_registry import visible_options
from app.command_mode import MODES
from app.config import settings
from app.guard.policies import policy_engine
from app.services.audit_stats import (
    hitl_latency_stats,
    load_audit_entries,
    top_risky_sessions,
    trend_by_day,
)
from app.services.llm_status import llm_snapshot

PAGES = [
    ("overview", "概览", "系统状态、运行配置与最近流量"),
    ("knowledge", "知识库", "文档上传、分块与检索库管理"),
    ("sessions", "会话", "活跃会话、模式与记忆管理"),
    ("test", "测试台", "向本机网关发送测试请求"),
    ("logs", "请求日志", "审计请求记录与详情"),
    ("security", "安全合规", "策略拦截、高风险会话与人工审批耗时"),
    ("ops", "运维", "Provider 配置、Feature Flag 与运行状态"),
    ("chat", "对话", "自主任务 Agent 对话界面"),
]

NAV_ICONS = {
    "overview": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
        '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>'
    ),
    "knowledge": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/>'
        '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/></svg>'
    ),
    "sessions": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5c-1.6 0-3.1-.4-4.4-1.2L3 20l1.2-5.1A8.5 8.5 0 1 1 21 11.5z"/>'
        '<path d="M8.5 11.5h.01M12.5 11.5h.01M16.5 11.5h.01"/></svg>'
    ),
    "test": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/></svg>'
    ),
    "logs": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/>'
        '<circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/></svg>'
    ),
    "security": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M12 2 4 5.5v5.6c0 5 3.4 9.6 8 10.9 4.6-1.3 8-5.9 8-10.9V5.5z"/>'
        '<path d="m9 11.5 2 2 4-4"/></svg>'
    ),
    "ops": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M21 4h-7"/><path d="M10 4H3"/><path d="M21 12h-9"/><path d="M8 12H3"/>'
        '<path d="M21 20h-5"/><path d="M12 20H3"/><circle cx="14" cy="4" r="2"/>'
        '<circle cx="8" cy="12" r="2"/><circle cx="16" cy="20" r="2"/></svg>'
    ),
    "chat": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'
        '<path d="M9 9h6M9 12h4"/></svg>'
    ),
}

HTML_SHELL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>{{title}} · Agent Gateway</title>
<link rel="stylesheet" href="/dashboard/static/tokens.css">
<link rel="stylesheet" href="/dashboard/static/dashboard.css">
</head>
<body data-page="{{page_key}}">
<div class="app-shell">
  <aside class="sidebar">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">M</span>
      <div class="brand-text"><strong>Agent Gateway</strong><span>v0.1.0 · 管理后台</span></div>
    </div>
    <nav class="nav" aria-label="主导航">{{nav}}</nav>
    <div class="sidebar-foot">{{auth_foot}}</div>
  </aside>
  <div class="main">
    <header class="topbar">
      <div>
        <h1 class="page-title">{{title}}</h1>
        <p class="page-sub">{{subtitle}}</p>
      </div>
      <div class="top-actions">
        <span class="health-chip" id="health-chip" data-tone="neutral">检查中</span>
        <button class="btn btn-ghost btn-sm" id="refresh-btn" type="button">刷新</button>
      </div>
    </header>
    <main class="content">{{content}}</main>
  </div>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script src="/dashboard/static/dashboard.js"></script>
<script src="/dashboard/static/chat.js"></script>
</body>
</html>
"""


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def fmt_ms(ms: float) -> str:
    return f"{ms / 1000:.1f} 秒 · {ms:.0f} ms"


def sev_badge(severity: str) -> str:
    color = {"high": "#e5484d", "medium": "#f5a524", "low": "#2f9e44"}.get(severity, "#6b7280")
    return (f'<span style="display:inline-block;padding:1px 10px;border-radius:10px;'
            f'font-size:12px;color:#fff;background:{color};">{esc(severity or "unknown")}</span>')


def bar_row(label: str, count: int, total: int, color: str) -> str:
    pct = round(count / total * 100) if total else 0
    return (
        '<div style="display:flex;align-items:center;gap:10px;margin:6px 0;font-size:13px;">'
        f'<span style="width:70px;flex:none;color:#6b7280;">{label}</span>'
        '<div style="flex:1;background:#f1f3f5;border-radius:4px;height:14px;overflow:hidden;">'
        f'<div style="width:{pct}%;height:100%;background:{color};border-radius:4px;"></div></div>'
        f'<span style="width:32px;flex:none;text-align:right;" class="mono">{count}</span>'
        f'<span style="width:40px;flex:none;color:#6b7280;">{pct}%</span></div>'
    )


def trend_rows(rows: list[dict[str, Any]]) -> str:
    return "\n".join(
        f'<tr><td class="mono">{esc(r["date"])}</td>'
        f'<td>{r["deny"]}</td><td>{r["review"]}</td></tr>'
        for r in rows
    ) or '<tr><td colspan="3" class="muted">暂无数据</td></tr>'


def trend_chart(rows: list[dict[str, Any]]) -> str:
    max_total = max((r["deny"] + r["review"] for r in rows), default=1) or 1
    parts = []
    for row in rows:
        deny_w = round(row["deny"] / max_total * 100)
        review_w = round(row["review"] / max_total * 100)
        parts.append(
            '<div style="display:flex;align-items:center;gap:10px;margin:6px 0;font-size:13px;">'
            f'<span style="width:70px;flex:none;color:#6b7280;">{esc(row["date"][5:])}</span>'
            '<span style="width:52px;flex:none;color:#6b7280;">deny</span>'
            '<div style="flex:1;background:#f1f3f5;border-radius:4px;height:16px;overflow:hidden;">'
            f'<div style="width:{deny_w}%;height:100%;background:#e5484d;border-radius:4px;"></div></div>'
            f'<span style="width:28px;flex:none;text-align:right;" class="mono">{row["deny"]}</span>'
            '<span style="width:64px;flex:none;color:#6b7280;">review</span>'
            '<div style="flex:1;background:#f1f3f5;border-radius:4px;height:16px;overflow:hidden;">'
            f'<div style="width:{review_w}%;height:100%;background:#f5a524;border-radius:4px;"></div></div>'
            f'<span style="width:28px;flex:none;text-align:right;" class="mono">{row["review"]}</span></div>'
        )
    return '<div style="margin-top:16px;">' + "\n".join(parts) + "</div>"


def risky_rows(rows: list[dict[str, Any]]) -> str:
    return "\n".join(
        f'<tr><td class="mono">{esc(r["session_id"])}</td>'
        f'<td>{r["count"]}</td>'
        f'<td class="muted">{esc(r["recent_violation"]) or "—"}</td></tr>'
        for r in rows
    )


def latency_block(stats: dict[str, Any]) -> str:
    if not stats["count"]:
        return '<p class="empty-state">暂无人工审批记录</p>'
    buckets = stats["buckets"]
    total = stats["count"]
    bars = (
        bar_row("0-30s", buckets["lt30"], total, "#2f9e44")
        + bar_row("30-120s", buckets["30to120"], total, "#f5a524")
        + bar_row("120s+", buckets["gt120"], total, "#e5484d")
    )
    return (
        '<div class="stat-band">'
        f'<div class="stat-card"><span class="stat-label">审批样本</span><strong class="stat-value">{stats["count"]}</strong></div>'
        f'<div class="stat-card"><span class="stat-label">P50</span><strong class="stat-value">{fmt_ms(stats["p50_ms"])}</strong></div>'
        f'<div class="stat-card"><span class="stat-label">P95</span><strong class="stat-value">{fmt_ms(stats["p95_ms"])}</strong></div>'
        f'<div class="stat-card"><span class="stat-label">最大</span><strong class="stat-value">{fmt_ms(stats["max_ms"])}</strong></div>'
        "</div>"
        '<div style="margin-top:16px;">' + bars + "</div>"
    )


def policy_rows() -> str:
    rows = []
    for policy in policy_engine.list():
        rows.append(
            f'<tr><td class="mono">{esc(policy.policy_id)}</td>'
            f'<td>{esc(policy.name)}</td>'
            f'<td>{sev_badge(policy.severity)}</td>'
            f'<td class="muted">{esc(policy.description)}</td></tr>'
        )
    return "\n".join(rows) or '<tr><td colspan="4" class="muted">暂无策略</td></tr>'


def render_shell(
    title: str,
    page_key: str,
    subtitle: str,
    content: str,
    active_key: str | None = None,
) -> HTMLResponse:
    nav_items = []
    for key, label, _ in PAGES:
        active = " is-active" if key == (active_key or page_key) else ""
        nav_items.append(
            f'<a class="nav-item{active}" href="/dashboard/{key}">'
            f'<span class="nav-icon">{NAV_ICONS[key]}</span><span>{label}</span></a>'
        )
    # 请求期读取（而非 import 期固化）：test_dashboard_routes 在运行时切换
    # 鉴权 env 并断言侧栏文案随真实配置变化。
    auth_enabled = bool(
        os.environ.get("DASHBOARD_PASSWORD", "")
        or os.environ.get("WEBHOOK_AUTH_TOKEN", "")
    )
    auth_foot = (
        "本地模式 · 鉴权已启用 · 数据仅本机可见"
        if auth_enabled
        else "本地模式 · 无鉴权 · 数据仅本机可见"
    )
    return HTMLResponse(
        HTML_SHELL
        .replace("{{title}}", title)
        .replace("{{page_key}}", page_key)
        .replace("{{subtitle}}", subtitle)
        .replace("{{nav}}", "\n".join(nav_items))
        .replace("{{auth_foot}}", auth_foot)
        .replace("{{content}}", content)
    )


def overview_page() -> str:
    _llm = llm_snapshot()
    model = str(_llm["model"]).strip() or "未设置"
    base_url = str(_llm["base_url"]).strip() or "未设置"
    feishu = "已配置" if settings.feishu_app_id else "未配置"
    return f"""
<section class="stat-band" aria-label="核心指标">
  <div class="stat-card"><span class="stat-label">系统状态</span><strong class="stat-value" id="stat-health">—</strong></div>
  <div class="stat-card"><span class="stat-label">Redis</span><strong class="stat-value" id="stat-redis">—</strong></div>
  <div class="stat-card"><span class="stat-label">活跃会话</span><strong class="stat-value" id="stat-sessions">—</strong></div>
  <div class="stat-card"><span class="stat-label">知识文档</span><strong class="stat-value" id="stat-docs">—</strong></div>
</section>

<section class="panel">
  <div class="panel-head"><h2>依赖检查</h2></div>
  <div class="check-list" id="health-detail">加载中…</div>
</section>

<section class="grid-2">
  <div class="panel">
    <div class="panel-head"><h2>运行配置</h2></div>
    <dl class="config-list">
      <div><dt>LLM 模型</dt><dd class="mono">{esc(model)}</dd></div>
      <div><dt>API 地址</dt><dd class="mono">{esc(base_url)}</dd></div>
      <div><dt>飞书卡片</dt><dd>{esc(feishu)}</dd></div>
    </dl>
  </div>
  <div class="panel">
    <div class="panel-head"><h2>快捷入口</h2></div>
    <div class="quick-links">
      <a class="quick-link" href="/dashboard/knowledge"><strong>知识库</strong><span>上传与管理文档</span></a>
      <a class="quick-link" href="/dashboard/sessions"><strong>会话</strong><span>查看活跃对话与模式</span></a>
      <a class="quick-link" href="/dashboard/test"><strong>测试台</strong><span>发送测试请求</span></a>
      <a class="quick-link" href="/dashboard/logs"><strong>请求日志</strong><span>审计记录与详情</span></a>
      <a class="quick-link" href="/dashboard/ops"><strong>运维</strong><span>Provider 与 Feature Flag</span></a>
    </div>
  </div>
</section>

<section class="panel">
  <div class="panel-head"><h2>最近请求</h2></div>
  <div id="recent-logs">加载中…</div>
</section>
"""


def knowledge_page() -> str:
    return """
<section class="panel">
  <div class="panel-head"><h2>上传文档</h2></div>
  <form id="kb-upload" class="form-stack">
    <label>文档标题
      <input id="kb-title" name="title" maxlength="80" required autocomplete="off" placeholder="例如：产品手册、FAQ">
    </label>
    <label>正文内容
      <textarea id="kb-content" name="content" rows="7" required placeholder="粘贴文档正文，保存后自动分块并写入检索库"></textarea>
    </label>
    <div class="form-row">
      <button id="kb-upload-btn" class="btn btn-primary" type="submit" data-loading-text="上传中…">上传文档</button>
      <span class="form-hint">文档按 500 字分块、重叠 50 字</span>
    </div>
  </form>
  <div class="file-upload-row">
    <label class="file-field">上传文件
      <input id="kb-file" type="file" accept=".txt,.md,.markdown,.json,.csv" class="input">
    </label>
    <button id="kb-file-btn" class="btn btn-ghost" type="button" data-loading-text="上传中…">上传文件</button>
  </div>
</section>
<section class="panel">
  <div class="panel-head"><h2>检索测试</h2></div>
  <form id="kb-search-form" class="form-stack">
    <label>查询内容
      <input id="kb-search-query" required autocomplete="off" placeholder="输入一个问题或关键词，测试知识库召回">
    </label>
    <div class="form-row">
      <button id="kb-search-btn" class="btn btn-primary" type="submit" data-loading-text="检索中…">开始检索</button>
      <span class="muted" id="kb-search-count"></span>
    </div>
  </form>
  <div id="kb-search-results" class="search-results"></div>
</section>
<section class="panel">
  <div class="panel-head"><h2>知识库文档</h2><span class="muted" id="kb-count"></span></div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>标题</th><th>ID</th><th>分块</th><th class="col-actions">操作</th></tr></thead>
      <tbody id="kb-body"></tbody>
    </table>
  </div>
</section>
"""


def sessions_page() -> str:
    return """
<section class="panel">
  <div class="panel-head"><h2>活跃会话</h2><span class="muted" id="session-count"></span></div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>会话 ID</th><th>模式</th><th>最近消息</th><th class="col-actions">操作</th></tr></thead>
      <tbody id="session-body"></tbody>
    </table>
  </div>
</section>
<p class="panel-note">清空会话会同时删除该会话的对话记忆与指令模式。</p>
"""


def test_page() -> str:
    return """
<section class="panel">
  <div class="panel-head"><h2>发送测试请求</h2></div>
  <form id="webhook-form" class="form-stack">
    <label>消息内容
      <input id="wh-text" value="hello" required autocomplete="off">
    </label>
    <div class="form-grid-2">
      <label>会话 ID <input id="wh-session" value="dash-test" autocomplete="off"></label>
      <label>用户 ID <input id="wh-user" value="admin" autocomplete="off"></label>
    </div>
    <div class="form-row">
      <button id="wh-send-btn" class="btn btn-primary" type="submit" data-loading-text="发送中…">发送测试请求</button>
    </div>
  </form>
  <div class="result-panel" id="wh-result" hidden>
    <div class="result-head"><span class="status-dot status-neutral"></span><strong id="wh-status">等待发送</strong></div>
    <pre id="wh-body"></pre>
  </div>
</section>
"""


def logs_page() -> str:
    return """
<section class="panel">
  <div class="panel-head">
    <h2>请求日志</h2>
    <div class="toolbar">
      <input id="log-filter" class="input input-sm" placeholder="搜索 intent / agent / 会话…">
      <span class="muted" id="log-count"></span>
    </div>
  </div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>时间</th><th>意图</th><th>Agent</th><th>会话</th><th>评分</th><th>输入</th><th>输出</th><th class="col-actions">操作</th></tr></thead>
      <tbody id="log-body"></tbody>
    </table>
  </div>
  <p class="empty-state" id="log-empty" hidden>暂无请求日志</p>
</section>
"""


def ops_page() -> str:
    provider_options = "\n".join(
        f'          <option value="{spec.value}">{spec.label}</option>'
        for spec in visible_options()
    )
    return f"""
<section class="panel">
  <div class="panel-head"><h2>Provider 配置</h2><span class="muted">运行时生效，重启后恢复环境变量</span></div>
  <form id="ops-config-form" class="form-stack">
    <div class="form-grid-2">
      <label>Provider
        <select id="ops-provider" class="input">
{provider_options}
        </select>
      </label>
      <label>模型
        <input id="ops-model" autocomplete="off" placeholder="例如 nvidia/nemotron-3-super-120b-a12b">
      </label>
    </div>
    <label>API 地址
      <input id="ops-base-url" autocomplete="off" placeholder="https://integrate.api.nvidia.com/v1">
    </label>
    <label>API Key
      <input id="ops-api-key" type="password" autocomplete="off" placeholder="留空则保持当前 Key">
    </label>
    <div class="form-row">
      <button id="ops-save-btn" class="btn btn-primary" type="submit" data-loading-text="保存中…">保存配置</button>
      <span class="muted" id="ops-key-status"></span>
    </div>
  </form>
  <div class="result-panel ops-test-panel" id="ops-test-result" hidden>
    <div class="result-head"><span class="status-dot status-neutral"></span><strong id="ops-test-status">等待测试</strong></div>
    <pre id="ops-test-body"></pre>
  </div>
  <div class="form-row ops-test-row">
    <input id="ops-test-message" class="input" value="ping" placeholder="测试消息内容">
    <button id="ops-test-btn" class="btn btn-ghost" type="button" data-loading-text="测试中…">发送测试消息</button>
  </div>
</section>
<section class="panel">
  <div class="panel-head"><h2>运行状态</h2>
    <div class="toolbar">
      <span class="muted" id="ops-obsidian-status"></span>
      <button id="obsidian-sync-btn" class="btn btn-ghost btn-sm" type="button" data-loading-text="同步中…">立即同步</button>
    </div>
  </div>
  <div class="check-list" id="ops-status">加载中…</div>
</section>
<section class="panel">
  <div class="panel-head"><h2>Feature Flags</h2><span class="muted">开关即时生效</span></div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>名称</th><th>值</th><th class="col-actions">操作</th></tr></thead>
      <tbody id="ops-flag-body"></tbody>
    </table>
  </div>
</section>
"""


def security_page() -> str:
    entries = load_audit_entries(7)
    trend = trend_by_day(entries, 7)
    risky = top_risky_sessions(entries, 10)
    latency = hitl_latency_stats(entries)
    empty_attr = "" if risky else " hidden"
    return f"""
<section class="panel">
  <div class="panel-head"><h2>拦截趋势</h2><span class="muted">近 7 天 deny / review 次数</span></div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>日期</th><th>Deny 拦截</th><th>Review 待审</th></tr></thead>
      <tbody>{trend_rows(trend)}</tbody>
    </table>
  </div>
  {trend_chart(trend)}
</section>

<section class="panel">
  <div class="panel-head"><h2>高风险会话 Top10</h2><span class="muted">按 deny / review 拦截次数聚合</span></div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>会话 ID</th><th>拦截次数</th><th>最近一次违规</th></tr></thead>
      <tbody>{risky_rows(risky)}</tbody>
    </table>
  </div>
  <p class="empty-state"{empty_attr}>暂无拦截记录</p>
</section>

<section class="panel">
  <div class="panel-head"><h2>人工审批耗时分布</h2><span class="muted">hitl_approve / hitl_reject 决策耗时</span></div>
  {latency_block(latency)}
</section>

<section class="panel">
  <div class="panel-head"><h2>策略清单</h2><span class="muted">策略在服务端统一注册，规则可配置</span></div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>策略 ID</th><th>名称</th><th>严重度</th><th>说明</th></tr></thead>
      <tbody>{policy_rows()}</tbody>
    </table>
  </div>
</section>
"""


def session_detail(sid: str, current_mode: str | None) -> str:
    options = "".join(
        f'<option value="{key}"{" selected" if current_mode == key else ""}>'
        f'{MODES.get(key, {}).get("label", key)}</option>'
        for key in MODES
    )
    return f"""
<a class="back-link" href="/dashboard/sessions">← 返回会话列表</a>
<div id="session-detail-root" data-sid="{esc(sid)}"></div>
<section class="panel">
  <div class="panel-head"><h2>会话详情</h2><span class="muted mono">{esc(sid)}</span></div>
  <div class="detail-actions">
    <label class="inline-field">当前模式
      <select id="session-mode-select">{options}</select>
    </label>
    <button id="session-mode-btn" class="btn btn-ghost" type="button">切换模式</button>
    <button id="session-clear-btn" class="btn btn-danger" type="button">清空会话</button>
  </div>
</section>
<section class="panel">
  <div class="panel-head"><h2>对话记录</h2><span class="muted" id="session-history-count"></span></div>
  <div id="session-history" class="history-list">加载中…</div>
</section>
"""


def knowledge_detail(doc_id: str) -> str:
    return f"""
<a class="back-link" href="/dashboard/knowledge">← 返回知识库</a>
<section class="panel">
  <div class="panel-head"><h2>文档详情</h2><span class="muted mono">{esc(doc_id)}</span></div>
  <div id="doc-detail-root" class="doc-detail" data-doc-id="{esc(doc_id)}">加载中…</div>
</section>
"""


def chat_page() -> str:
    return """
<section class="chat-root">
  <div class="chat-sidebar">
    <div class="chat-sid-label">会话 ID</div>
    <div class="mono chat-sid-value" id="chat-sid">—</div>
    <div class="chat-sid-label">用户 ID</div>
    <input class="input input-sm" id="chat-uid" value="web-user" autocomplete="off" aria-label="用户 ID">
    <button class="btn btn-ghost btn-sm" id="chat-reset-btn" type="button">重置会话</button>
  </div>
  <div class="chat-main">
    <div class="chat-messages" id="chat-messages">
      <div class="empty-state">还没有对话，输入任务开始吧<br>（例如：帮我算 3*7 并记下来）</div>
    </div>
    <form class="chat-form" id="chat-form">
      <input id="chat-input" class="input chat-input" placeholder="输入任务…" autocomplete="off" autofocus>
      <button class="btn btn-primary" id="chat-send-btn" type="submit">发送</button>
    </form>
  </div>
</section>
"""


def render_page(page_key: str) -> HTMLResponse:
    label, subtitle = {key: (label, subtitle) for key, label, subtitle in PAGES}[page_key]
    content = {
        "overview": overview_page,
        "knowledge": knowledge_page,
        "sessions": sessions_page,
        "test": test_page,
        "logs": logs_page,
        "security": security_page,
        "ops": ops_page,
        "chat": chat_page,
    }[page_key]()
    return render_shell(label, page_key, subtitle, content)


__all__ = [
    "HTML_SHELL",
    "NAV_ICONS",
    "PAGES",
    "bar_row",
    "chat_page",
    "esc",
    "fmt_ms",
    "knowledge_detail",
    "knowledge_page",
    "latency_block",
    "logs_page",
    "ops_page",
    "overview_page",
    "policy_rows",
    "render_page",
    "render_shell",
    "risky_rows",
    "security_page",
    "session_detail",
    "sev_badge",
    "sessions_page",
    "test_page",
    "trend_chart",
    "trend_rows",
]
