from __future__ import annotations

import datetime
import html
import json
import os
import pathlib
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.agents.provider import LLMClient, LLMConfig
from app.command_mode import MODES
from app.config import settings
from app.deps import _flag_client, _retriever, command_mode, knowledge_base, memory, obsidian_sync
from app.feature_flags import DEFAULT_FLAGS
from app.guard.policies import policy_engine

router = APIRouter()

PAGES = [
    ("overview", "概览", "系统状态、运行配置与最近流量"),
    ("knowledge", "知识库", "文档上传、分块与检索库管理"),
    ("sessions", "会话", "活跃会话、模式与记忆管理"),
    ("test", "测试台", "向本机网关发送测试请求"),
    ("logs", "请求日志", "审计请求记录与详情"),
    ("security", "安全合规", "策略拦截、高风险会话与人工审批耗时"),
    ("ops", "运维", "Provider 配置、Feature Flag 与运行状态"),
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
}

HTML_SHELL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{title}} · MoA Gateway</title>
<link rel="stylesheet" href="/dashboard/static/tokens.css">
<link rel="stylesheet" href="/dashboard/static/dashboard.css">
</head>
<body data-page="{{page_key}}">
<div class="app-shell">
  <aside class="sidebar">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">M</span>
      <div class="brand-text"><strong>MoA Gateway</strong><span>v0.1.0 · 管理后台</span></div>
    </div>
    <nav class="nav" aria-label="主导航">{{nav}}</nav>
    <div class="sidebar-foot">本地模式 · 无鉴权 · 数据仅本机可见</div>
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
</body>
</html>
"""


def _read_recent_logs(count: int = 50) -> list[dict[str, Any]]:
    log_dir = pathlib.Path("logs")
    if not log_dir.exists():
        return []
    files = sorted(log_dir.glob("audit-*.jsonl"), key=lambda p: p.name, reverse=True)[:3]
    entries: list[dict[str, Any]] = []
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries[:count]


def _load_audit_entries(days: int = 7) -> list[dict[str, Any]]:
    log_dir = pathlib.Path("logs")
    if not log_dir.exists():
        return []
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days - 1)
    entries: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("audit-*.jsonl"), key=lambda p: p.name, reverse=True):
        try:
            file_date = datetime.date.fromisoformat(path.name[len("audit-"):-len(".jsonl")])
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            ts = data.get("timestamp", "")
            if not ts:
                continue
            try:
                datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            entries.append(data)
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries


def _trend_by_day(entries: list[dict[str, Any]], days: int = 7) -> list[dict[str, Any]]:
    today = datetime.date.today()
    dates = [(today - datetime.timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    buckets = {d: {"date": d, "deny": 0, "review": 0} for d in dates}
    for entry in entries:
        ts = entry.get("timestamp", "")
        try:
            day = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date().isoformat()
        except (TypeError, ValueError):
            continue
        if day not in buckets:
            continue
        action = str(entry.get("guard_action", ""))
        if action == "deny":
            buckets[day]["deny"] += 1
        elif action == "review":
            buckets[day]["review"] += 1
    return [buckets[d] for d in dates]


def _top_risky_sessions(entries: list[dict[str, Any]], top_n: int = 10) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for entry in entries:
        action = str(entry.get("guard_action", ""))
        if action not in ("deny", "review"):
            continue
        sid = str(entry.get("session_id", "") or "unknown")
        item = agg.setdefault(sid, {"session_id": sid, "count": 0, "timestamp": "", "recent_violation": ""})
        item["count"] += 1
        ts = str(entry.get("timestamp", ""))
        if ts > item["timestamp"]:
            item["timestamp"] = ts
            hits = entry.get("policy_hits")
            if isinstance(hits, list) and hits:
                item["recent_violation"] = str(hits[0])
            else:
                item["recent_violation"] = str(entry.get("violation", ""))
    rows = sorted(agg.values(), key=lambda r: (-r["count"], r["session_id"]))[:top_n]
    for row in rows:
        row.pop("timestamp", None)
    return rows


def _percentile(sorted_values: list[float], p: int) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    pos = (p / 100.0) * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _hitl_latency_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    durations: list[float] = []
    for entry in entries:
        if not entry.get("hitl_decision"):
            continue
        try:
            value = float(entry.get("hitl_duration_ms", 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            durations.append(value)
    if not durations:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0,
                "buckets": {"lt30": 0, "30to120": 0, "gt120": 0}}
    sorted_d = sorted(durations)
    return {
        "count": len(sorted_d),
        "p50_ms": round(_percentile(sorted_d, 50), 1),
        "p95_ms": round(_percentile(sorted_d, 95), 1),
        "max_ms": round(sorted_d[-1], 1),
        "buckets": {
            "lt30": sum(1 for d in sorted_d if d < 30_000),
            "30to120": sum(1 for d in sorted_d if 30_000 <= d < 120_000),
            "gt120": sum(1 for d in sorted_d if d >= 120_000),
        },
    }


def _fmt_ms(ms: float) -> str:
    return f"{ms / 1000:.1f} 秒 · {ms:.0f} ms"


def _sev_badge(severity: str) -> str:
    color = {"high": "#e5484d", "medium": "#f5a524", "low": "#2f9e44"}.get(severity, "#6b7280")
    return (f'<span style="display:inline-block;padding:1px 10px;border-radius:10px;'
            f'font-size:12px;color:#fff;background:{color};">{_esc(severity or "unknown")}</span>')


def _bar_row(label: str, count: int, total: int, color: str) -> str:
    pct = round(count / total * 100) if total else 0
    return (
        '<div style="display:flex;align-items:center;gap:10px;margin:6px 0;font-size:13px;">'
        f'<span style="width:70px;flex:none;color:#6b7280;">{label}</span>'
        '<div style="flex:1;background:#f1f3f5;border-radius:4px;height:14px;overflow:hidden;">'
        f'<div style="width:{pct}%;height:100%;background:{color};border-radius:4px;"></div></div>'
        f'<span style="width:32px;flex:none;text-align:right;" class="mono">{count}</span>'
        f'<span style="width:40px;flex:none;color:#6b7280;">{pct}%</span></div>'
    )


def _trend_rows(rows: list[dict[str, Any]]) -> str:
    return "\n".join(
        f'<tr><td class="mono">{_esc(r["date"])}</td>'
        f'<td>{r["deny"]}</td><td>{r["review"]}</td></tr>'
        for r in rows
    ) or '<tr><td colspan="3" class="muted">暂无数据</td></tr>'


def _trend_chart(rows: list[dict[str, Any]]) -> str:
    max_total = max((r["deny"] + r["review"] for r in rows), default=1) or 1
    parts = []
    for row in rows:
        deny_w = round(row["deny"] / max_total * 100)
        review_w = round(row["review"] / max_total * 100)
        parts.append(
            '<div style="display:flex;align-items:center;gap:10px;margin:6px 0;font-size:13px;">'
            f'<span style="width:70px;flex:none;color:#6b7280;">{_esc(row["date"][5:])}</span>'
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


def _risky_rows(rows: list[dict[str, Any]]) -> str:
    return "\n".join(
        f'<tr><td class="mono">{_esc(r["session_id"])}</td>'
        f'<td>{r["count"]}</td>'
        f'<td class="muted">{_esc(r["recent_violation"]) or "—"}</td></tr>'
        for r in rows
    )


def _latency_block(stats: dict[str, Any]) -> str:
    if not stats["count"]:
        return '<p class="empty-state">暂无人工审批记录</p>'
    buckets = stats["buckets"]
    total = stats["count"]
    bars = (
        _bar_row("0-30s", buckets["lt30"], total, "#2f9e44")
        + _bar_row("30-120s", buckets["30to120"], total, "#f5a524")
        + _bar_row("120s+", buckets["gt120"], total, "#e5484d")
    )
    return (
        '<div class="stat-band">'
        f'<div class="stat-card"><span class="stat-label">审批样本</span><strong class="stat-value">{stats["count"]}</strong></div>'
        f'<div class="stat-card"><span class="stat-label">P50</span><strong class="stat-value">{_fmt_ms(stats["p50_ms"])}</strong></div>'
        f'<div class="stat-card"><span class="stat-label">P95</span><strong class="stat-value">{_fmt_ms(stats["p95_ms"])}</strong></div>'
        f'<div class="stat-card"><span class="stat-label">最大</span><strong class="stat-value">{_fmt_ms(stats["max_ms"])}</strong></div>'
        "</div>"
        '<div style="margin-top:16px;">' + bars + "</div>"
    )


def _policy_rows() -> str:
    rows = []
    for policy in policy_engine.list():
        rows.append(
            f'<tr><td class="mono">{_esc(policy.policy_id)}</td>'
            f'<td>{_esc(policy.name)}</td>'
            f'<td>{_sev_badge(policy.severity)}</td>'
            f'<td class="muted">{_esc(policy.description)}</td></tr>'
        )
    return "\n".join(rows) or '<tr><td colspan="4" class="muted">暂无策略</td></tr>'


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _shell(
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
    return HTMLResponse(
        HTML_SHELL
        .replace("{{title}}", title)
        .replace("{{page_key}}", page_key)
        .replace("{{subtitle}}", subtitle)
        .replace("{{nav}}", "\n".join(nav_items))
        .replace("{{content}}", content)
    )


def _overview() -> str:
    model = os.environ.get("LLM_MODEL", "").strip() or "未设置"
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "未设置"
    feishu = "已配置" if os.environ.get("FEISHU_APP_ID", "") else "未配置"
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
      <div><dt>LLM 模型</dt><dd class="mono">{_esc(model)}</dd></div>
      <div><dt>API 地址</dt><dd class="mono">{_esc(base_url)}</dd></div>
      <div><dt>飞书卡片</dt><dd>{_esc(feishu)}</dd></div>
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


def _knowledge() -> str:
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


def _sessions() -> str:
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


def _test() -> str:
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


def _logs() -> str:
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


def _ops() -> str:
    return """
<section class="panel">
  <div class="panel-head"><h2>Provider 配置</h2><span class="muted">运行时生效，重启后恢复环境变量</span></div>
  <form id="ops-config-form" class="form-stack">
    <div class="form-grid-2">
      <label>模型
        <input id="ops-model" autocomplete="off" placeholder="例如 deepseek-chat / auto">
      </label>
      <label>API 地址
        <input id="ops-base-url" autocomplete="off" placeholder="https://api.deepseek.com/v1">
      </label>
    </div>
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


def _security() -> str:
    entries = _load_audit_entries(7)
    trend = _trend_by_day(entries, 7)
    risky = _top_risky_sessions(entries, 10)
    latency = _hitl_latency_stats(entries)
    empty_attr = "" if risky else " hidden"
    return f"""
<section class="panel">
  <div class="panel-head"><h2>拦截趋势</h2><span class="muted">近 7 天 deny / review 次数</span></div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>日期</th><th>Deny 拦截</th><th>Review 待审</th></tr></thead>
      <tbody>{_trend_rows(trend)}</tbody>
    </table>
  </div>
  {_trend_chart(trend)}
</section>

<section class="panel">
  <div class="panel-head"><h2>高风险会话 Top10</h2><span class="muted">按 deny / review 拦截次数聚合</span></div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>会话 ID</th><th>拦截次数</th><th>最近一次违规</th></tr></thead>
      <tbody>{_risky_rows(risky)}</tbody>
    </table>
  </div>
  <p class="empty-state"{empty_attr}>暂无拦截记录</p>
</section>

<section class="panel">
  <div class="panel-head"><h2>人工审批耗时分布</h2><span class="muted">hitl_approve / hitl_reject 决策耗时</span></div>
  {_latency_block(latency)}
</section>

<section class="panel">
  <div class="panel-head"><h2>策略清单</h2><span class="muted">策略在服务端统一注册，规则可配置</span></div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>策略 ID</th><th>名称</th><th>严重度</th><th>说明</th></tr></thead>
      <tbody>{_policy_rows()}</tbody>
    </table>
  </div>
</section>
"""


def _session_detail(sid: str) -> str:
    options = "".join(
        f'<option value="{key}"{" selected" if command_mode.get(sid) == key else ""}>'
        f'{MODES.get(key, {}).get("label", key)}</option>'
        for key in MODES
    )
    return f"""
<a class="back-link" href="/dashboard/sessions">← 返回会话列表</a>
<div id="session-detail-root" data-sid="{_esc(sid)}"></div>
<section class="panel">
  <div class="panel-head"><h2>会话详情</h2><span class="muted mono">{_esc(sid)}</span></div>
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


def _knowledge_detail(doc_id: str) -> str:
    return f"""
<a class="back-link" href="/dashboard/knowledge">← 返回知识库</a>
<section class="panel">
  <div class="panel-head"><h2>文档详情</h2><span class="muted mono">{_esc(doc_id)}</span></div>
  <div id="doc-detail-root" class="doc-detail" data-doc-id="{_esc(doc_id)}">加载中…</div>
</section>
"""


def _render(page_key: str) -> HTMLResponse:
    label, subtitle = {key: (label, subtitle) for key, label, subtitle in PAGES}[page_key]
    content = {
        "overview": _overview,
        "knowledge": _knowledge,
        "sessions": _sessions,
        "test": _test,
        "logs": _logs,
        "security": _security,
        "ops": _ops,
    }[page_key]()
    return _shell(label, page_key, subtitle, content)


class UploadDoc(BaseModel):
    title: str
    content: str


class ModeUpdate(BaseModel):
    mode: str


class SearchQuery(BaseModel):
    query: str
    top_k: int = 5


class OpsConfigUpdate(BaseModel):
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class OpsTestRequest(BaseModel):
    message: str = "ping"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class FlagUpdate(BaseModel):
    value: Any


@router.post("/dashboard/upload")
async def dashboard_upload(req: UploadDoc) -> JSONResponse:
    doc_id = await knowledge_base.add_document(req.title, req.content)
    return JSONResponse({"id": doc_id, "status": "ok"})


@router.post("/dashboard/delete")
async def dashboard_delete(body: dict) -> JSONResponse:
    doc_id = body.get("doc_id", "")
    ok = await knowledge_base.delete_doc(doc_id)
    return JSONResponse({"ok": ok})


@router.post("/dashboard/api/knowledge/upload_file")
async def dashboard_upload_file(
    file: UploadFile = File(...),
    title: str = Form(""),
) -> JSONResponse:
    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
    if not content.strip():
        return JSONResponse({"error": "empty file"}, status_code=400)
    name = (title or file.filename or "document").strip()
    doc_id = await knowledge_base.add_document(name, content)
    return JSONResponse({"id": doc_id, "title": name, "status": "ok"})


@router.get("/dashboard/api/knowledge/{doc_id}")
async def dashboard_knowledge_detail(doc_id: str) -> JSONResponse:
    doc = await knowledge_base.get_doc(doc_id)
    if doc is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"doc": doc})


@router.post("/dashboard/api/knowledge/search")
async def dashboard_knowledge_search(body: SearchQuery) -> JSONResponse:
    if not body.query.strip():
        return JSONResponse({"error": "query required"}, status_code=400)
    result = await _retriever.retrieve(body.query)
    hits = [{"chunk": chunk, "index": i} for i, chunk in enumerate(result.chunks, start=1)]
    return JSONResponse({"hits": hits, "doc_count": result.doc_count})


@router.get("/dashboard/api/sessions")
async def dashboard_sessions() -> JSONResponse:
    sessions = []
    seen: set[str] = set()
    for sid in memory.list_sessions():
        seen.add(sid)
        full = memory.get_history(sid, limit=50)
        history = full[-6:]
        mode = command_mode.get(sid) or "default"
        sessions.append({
            "id": sid,
            "session_id": sid[:16],
            "mode": mode,
            "mode_label": MODES.get(mode, {}).get("label", mode),
            "message_count": len(full),
            "history": [{"role": h["role"], "content": h["content"][:80]} for h in history],
        })
    for sid in list(command_mode._store.keys()):
        if sid not in seen:
            mode = command_mode.get(sid) or "default"
            sessions.append({
                "id": sid,
                "session_id": sid[:16],
                "mode": mode,
                "mode_label": MODES.get(mode, {}).get("label", mode),
                "message_count": 0,
                "history": [],
            })
    return JSONResponse({"sessions": sessions})


@router.get("/dashboard/api/sessions/{session_id}")
async def dashboard_session_detail(session_id: str) -> JSONResponse:
    mode = command_mode.get(session_id) or "default"
    history = memory.get_history(session_id, limit=50)
    return JSONResponse({
        "id": session_id,
        "mode": mode,
        "mode_label": MODES.get(mode, {}).get("label", mode),
        "history": [{"role": h["role"], "content": h["content"]} for h in history],
    })


@router.post("/dashboard/api/sessions/{session_id}/mode")
async def dashboard_session_mode(session_id: str, body: ModeUpdate) -> JSONResponse:
    mode = body.mode.strip().lower()
    if mode not in MODES:
        return JSONResponse({"ok": False, "error": f"unknown mode: {mode}"}, status_code=400)
    command_mode.set(session_id, mode)
    return JSONResponse({"ok": True, "mode": mode, "mode_label": MODES[mode]["label"]})


@router.post("/dashboard/api/sessions/{session_id}/clear")
async def dashboard_clear_session(session_id: str) -> JSONResponse:
    memory.clear(session_id)
    command_mode.clear(session_id)
    return JSONResponse({"ok": True, "session_id": session_id})


@router.get("/dashboard/api/logs")
async def dashboard_logs() -> JSONResponse:
    return JSONResponse({"logs": _read_recent_logs(100)})


@router.get("/dashboard/api/ops/config")
async def dashboard_ops_config() -> JSONResponse:
    flag_names = set(DEFAULT_FLAGS.keys())
    for key in _flag_client._store.keys():
        flag_names.add(key.removeprefix("moa:flag:"))
    flags = []
    for name in sorted(flag_names):
        value = await _flag_client.get(name, DEFAULT_FLAGS.get(name, False))
        flags.append({"name": name, "value": value})
    return JSONResponse({
        "llm": {
            "model": os.environ.get("LLM_MODEL", ""),
            "base_url": os.environ.get("OPENAI_BASE_URL", ""),
            "api_key_set": bool(os.environ.get("OPENAI_API_KEY", "")),
        },
        "feishu": {"configured": bool(os.environ.get("FEISHU_APP_ID", ""))},
        "redis": {"url": settings.redis_url},
        "tracing": {"otlp_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")},
        "limiter": {"wired": False, "note": "未接入全局限流"},
        "obsidian": obsidian_sync.status(),
        "flags": flags,
    })


@router.post("/dashboard/api/ops/config")
async def dashboard_ops_config_update(body: OpsConfigUpdate) -> JSONResponse:
    if body.model is not None:
        os.environ["LLM_MODEL"] = body.model.strip()
    if body.base_url is not None:
        os.environ["OPENAI_BASE_URL"] = body.base_url.strip().rstrip("/")
    if body.api_key is not None:
        os.environ["OPENAI_API_KEY"] = body.api_key.strip()
    return JSONResponse({
        "ok": True,
        "llm": {
            "model": os.environ.get("LLM_MODEL", ""),
            "base_url": os.environ.get("OPENAI_BASE_URL", ""),
            "api_key_set": bool(os.environ.get("OPENAI_API_KEY", "")),
        },
    })


@router.post("/dashboard/api/ops/test")
async def dashboard_ops_test(body: OpsTestRequest) -> JSONResponse:
    config = LLMConfig(
        api_key=(body.api_key or os.environ.get("OPENAI_API_KEY", "")).strip(),
        base_url=(body.base_url or os.environ.get("OPENAI_BASE_URL", "")).strip() or "https://api.openai.com/v1",
        model=(body.model or os.environ.get("LLM_MODEL", "")).strip() or "gpt-4o-mini",
        timeout=30.0,
        max_tokens=64,
    )
    client = LLMClient(config)
    try:
        reply = await client.chat(
            [{"role": "user", "content": (body.message or "ping")[:500]}],
            max_tokens=64,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:500]})
    finally:
        await client.aclose()
    return JSONResponse({"ok": True, "reply": reply[:2000]})


@router.post("/dashboard/api/ops/flags/{name}")
async def dashboard_flag_set(name: str, body: FlagUpdate) -> JSONResponse:
    await _flag_client.set(name, body.value)
    value = await _flag_client.get(name)
    return JSONResponse({"ok": True, "name": name, "value": value})


@router.delete("/dashboard/api/ops/flags/{name}")
async def dashboard_flag_delete(name: str) -> JSONResponse:
    await _flag_client.delete(name)
    return JSONResponse({"ok": True, "name": name})


@router.post("/dashboard/api/ops/obsidian/sync")
async def dashboard_obsidian_sync() -> JSONResponse:
    changed = await obsidian_sync.sync_once()
    return JSONResponse({"ok": True, "changed": changed, **obsidian_sync.status()})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_overview() -> HTMLResponse:
    return _render("overview")


@router.get("/dashboard/overview", response_class=HTMLResponse)
async def dashboard_overview_page() -> HTMLResponse:
    return _render("overview")


@router.get("/dashboard/knowledge", response_class=HTMLResponse)
async def dashboard_knowledge_page() -> HTMLResponse:
    return _render("knowledge")


@router.get("/dashboard/sessions", response_class=HTMLResponse)
async def dashboard_sessions_page() -> HTMLResponse:
    return _render("sessions")


@router.get("/dashboard/test", response_class=HTMLResponse)
async def dashboard_test_page() -> HTMLResponse:
    return _render("test")


@router.get("/dashboard/logs", response_class=HTMLResponse)
async def dashboard_logs_page() -> HTMLResponse:
    return _render("logs")


@router.get("/dashboard/security", response_class=HTMLResponse)
async def dashboard_security_page() -> HTMLResponse:
    return _render("security")


@router.get("/dashboard/ops", response_class=HTMLResponse)
async def dashboard_ops_page() -> HTMLResponse:
    return _render("ops")


@router.get("/dashboard/sessions/{session_id}", response_class=HTMLResponse)
async def dashboard_session_detail_page(session_id: str) -> HTMLResponse:
    return _shell(
        "会话详情",
        "session-detail",
        "完整对话记录与模式管理",
        _session_detail(session_id),
        active_key="sessions",
    )


@router.get("/dashboard/knowledge/{doc_id}", response_class=HTMLResponse)
async def dashboard_knowledge_detail_page(doc_id: str) -> HTMLResponse:
    return _shell(
        "文档详情",
        "knowledge-detail",
        "文档内容与分块详情",
        _knowledge_detail(doc_id),
        active_key="knowledge",
    )
