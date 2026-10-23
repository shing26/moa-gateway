"""Read-only HTML views for persisted multi-agent experiment artifacts."""

from __future__ import annotations

import html
import json
from typing import Any

from app.rendering.dashboard_html import esc, render_shell


def _int(value: Any) -> str:
    return f"{int(value or 0):,}"


def _float(value: Any, digits: int = 0) -> str:
    return f"{float(value or 0):,.{digits}f}"


def _cost(value: Any) -> str:
    return "-" if value is None else f"${float(value):,.4f}"


def _rate(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.0f}%"


def _status(value: Any) -> str:
    label = str(value or "unknown")
    tone = {
        "completed": "good",
        "pass": "good",
        "running": "info",
        "paused": "warn",
        "fail": "warn",
        "error": "bad",
        "completed_with_errors": "bad",
    }.get(label, "neutral")
    return f'<span class="status-pill" data-tone="{tone}">{esc(label)}</span>'


def _json_block(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return f"<pre>{html.escape(text)}</pre>"


def batches_page(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return """
<section class="panel">
  <div class="panel-head"><h2>实验批次</h2></div>
  <p class="empty-state">还没有批次产物。请先从 CLI 执行实验并写入
  <code>.bench-experiments</code>。</p>
</section>
"""
    body = "\n".join(
        "<tr>"
        f'<td data-label="批次"><a class="text-link mono" href="/dashboard/experiments/{esc(row["batch_id"])}">'
        f'{esc(row["batch_id"])}</a></td>'
        f'<td data-label="阶段">{esc(row["phase"])}</td>'
        f'<td data-label="预算档">{esc(row["budget_tier"])}</td>'
        f'<td data-label="状态">{_status(row["status"])}</td>'
        f'<td data-label="单元" class="mono">{_int(row["completed_cells"])} / {_int(row["cell_count"])}</td>'
        f'<td data-label="Runs" class="mono">{_int(row["completed_runs"])} / {_int(row["expected_runs"])}</td>'
        f'<td data-label="缺失" class="mono">{_int(row["missing_runs"])}</td>'
        f'<td data-label="更新时间" class="mono">{esc(row["updated_at"])}</td>'
        "</tr>"
        for row in rows
    )
    return f"""
<section class="panel">
  <div class="panel-head">
    <h2>实验批次</h2>
    <span class="muted">只读视图，不提供启动、重跑或修改入口</span>
  </div>
  <div class="table-wrap table-wrap-responsive">
    <table class="data-table responsive-stack">
      <thead><tr><th>批次</th><th>阶段</th><th>预算档</th><th>状态</th>
      <th>单元</th><th>Runs</th><th>缺失</th><th>更新时间</th></tr></thead>
      <tbody>{body}</tbody>
    </table>
  </div>
</section>
"""


def batch_page(payload: dict[str, Any]) -> str:
    manifest = dict(payload.get("manifest") or {})
    report = dict(payload.get("report") or {})
    dose = dict(report.get("report") or {})
    cells = list(payload.get("cells") or [])
    batch_id = str(manifest.get("batch_id", ""))
    max_concurrency = int(manifest.get("max_concurrency", 1) or 1)
    rate_limit = int(manifest.get("rate_limit_per_minute", 0) or 0)
    rate_limit_label = f"{rate_limit:,}/min" if rate_limit else "不限速"
    sample_target = int(
        manifest.get("target_trials_per_cell")
        or manifest.get("repeats")
        or manifest.get("expected_trials_per_cell")
        or 1
    )
    report_cells = {
        (str(item.get("task_id", "")), str(item.get("arm", ""))): item
        for item in (dose.get("cells") or [])
        if isinstance(item, dict)
    }
    rows: list[str] = []
    for raw in cells:
        if not isinstance(raw, dict):
            continue
        cell = dict(raw)
        key = (str(cell.get("task_id", "")), str(cell.get("arm", "")))
        metrics = dict(report_cells.get(key) or {})
        run_links = "<br>".join(
            f'<a class="text-link mono" href="/dashboard/experiments/{esc(batch_id)}/runs/'
            f'{esc(cell.get("experiment_id", ""))}/{int(run.get("trial_index", 0) or 0)}">'
            f'trial-{int(run.get("trial_index", 0) or 0):03d}</a> '
            f'({esc(run.get("status", ""))})'
            for run in list(cell.get("runs") or [])
            if isinstance(run, dict)
        ) or '<span class="muted">无记录</span>'
        rows.append(
            "<tr>"
            f'<td data-label="任务" class="mono">{esc(cell.get("task_id", ""))}</td>'
            f'<td data-label="Arm">{esc(cell.get("arm", ""))}</td>'
            f'<td data-label="状态">{_status(cell.get("status", ""))}</td>'
            f'<td data-label="样本量" class="mono">{_int(metrics.get("runs"))} / {sample_target}</td>'
            f'<td data-label="通过率">{_rate(metrics.get("pass_rate"))}</td>'
            f'<td data-label="Error" class="mono">{_int(metrics.get("error"))}</td>'
            f'<td data-label="预算耗尽" class="mono">{_int(metrics.get("budget_exhausted"))}</td>'
            f'<td data-label="平均成本" class="mono">{_cost(metrics.get("avg_cost_usd"))}</td>'
            f'<td data-label="平均 token" class="mono">{_float(metrics.get("avg_tokens"))}</td>'
            f'<td data-label="记录">{run_links}</td>'
            "</tr>"
        )

    gaps = list(dose.get("gaps") or [])
    under_sampled = list(dose.get("under_sampled") or [])
    warning = ""
    if report.get("statistical_claims") is False:
        warning = (
            '<p class="panel-note">统计声明未授权：结论受样本量、缺口和错误分类约束。</p>'
        )
    source = manifest.get("source_batch_id") or ""
    source_line = (
        f'<span class="muted">B1 来源：<span class="mono">{esc(source)}</span></span>'
        if source
        else ""
    )
    return f"""
<a class="back-link" href="/dashboard/experiments">← 返回实验批次</a>
<section class="panel">
  <div class="panel-head">
    <h2 class="mono">{esc(batch_id)}</h2>
    {_status(manifest.get("status", ""))}
  </div>
  <div class="stat-band">
    <div class="stat-card"><span class="stat-label">协议</span>
      <strong class="stat-value">{esc(manifest.get("protocol_id", ""))} v{esc(manifest.get("protocol_version", ""))}</strong></div>
    <div class="stat-card"><span class="stat-label">阶段 / 预算</span>
      <strong class="stat-value">{esc(manifest.get("phase", ""))} / {esc(manifest.get("budget_tier", "B1"))}</strong></div>
    <div class="stat-card"><span class="stat-label">完成 Runs</span>
      <strong class="stat-value">{_int(report.get("completed_runs"))} / {_int(report.get("expected_runs"))}</strong></div>
    <div class="stat-card"><span class="stat-label">缺失 / 缺口</span>
      <strong class="stat-value">{_int(report.get("missing_runs"))} / {len(gaps)}</strong></div>
    <div class="stat-card"><span class="stat-label">并发上限</span>
      <strong class="stat-value">{max_concurrency}</strong></div>
    <div class="stat-card"><span class="stat-label">启动速率</span>
      <strong class="stat-value">{esc(rate_limit_label)}</strong></div>
  </div>
  {source_line}
  <p class="muted">协议哈希 <span class="mono">{esc(manifest.get("protocol_hash", ""))}</span></p>
  {warning}
</section>
<section class="panel">
  <div class="panel-head"><h2>单元与 Run Record</h2>
    <span class="muted">缺口 {len(gaps)} · 样本不足 {len(under_sampled)}</span>
  </div>
  <div class="table-wrap table-wrap-responsive">
    <table class="data-table responsive-stack">
      <thead><tr><th>任务</th><th>Arm</th><th>状态</th><th>样本量</th><th>通过率</th>
      <th>Error</th><th>预算耗尽</th><th>平均成本</th><th>平均 token</th><th>记录</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="10" class="muted">暂无单元</td></tr>'}</tbody>
    </table>
  </div>
</section>
"""


def run_page(record: dict[str, Any], batch_id: str) -> str:
    metrics = dict(record.get("metrics") or {})
    evidence = dict(record.get("evidence") or {})
    model = dict(record.get("model") or {})
    steps = list(evidence.get("steps") or [])
    step_rows = "\n".join(
        "<tr>"
        f'<td data-label="#" class="mono">{int(step.get("index", 0) or 0)}</td>'
        f'<td data-label="动作">{esc(step.get("action", ""))}</td>'
        f'<td data-label="工具">{esc(step.get("tool", ""))}</td>'
        f'<td data-label="结果">{_status(step.get("outcome", ""))}</td>'
        f'<td data-label="耗时" class="mono">{_float(step.get("elapsed_ms"), 1)} ms</td>'
        "</tr>"
        for step in steps
        if isinstance(step, dict)
    )
    return f"""
<a class="back-link" href="/dashboard/experiments/{esc(batch_id)}">← 返回批次</a>
<section class="panel">
  <div class="panel-head">
    <h2 class="mono">{esc(record.get("experiment_id", ""))} / trial-{int(record.get("trial_index", 0) or 0):03d}</h2>
    {_status(record.get("status", ""))}
  </div>
  <div class="stat-band">
    <div class="stat-card"><span class="stat-label">任务</span>
      <strong class="stat-value mono">{esc(record.get("task", ""))}</strong></div>
    <div class="stat-card"><span class="stat-label">模型</span>
      <strong class="stat-value">{esc(model.get("model", ""))}</strong></div>
    <div class="stat-card"><span class="stat-label">Token</span>
      <strong class="stat-value">{_int(metrics.get("total_tokens"))}</strong></div>
    <div class="stat-card"><span class="stat-label">工具调用</span>
      <strong class="stat-value">{_int(metrics.get("tool_calls"))}</strong></div>
  </div>
  <p class="muted">证据文件 <span class="mono">{esc(record.get("_relative_path", ""))}</span></p>
  <p class="muted">Config hash <span class="mono">{esc(record.get("config_hash", ""))}</span></p>
  {'<p class="panel-note">' + esc(record.get("error", "")) + '</p>' if record.get("error") else ''}
</section>
<section class="panel">
  <div class="panel-head"><h2>工具轨迹</h2><span class="muted">{len(steps)} steps</span></div>
  <div class="table-wrap table-wrap-responsive">
    <table class="data-table responsive-stack">
      <thead><tr><th>#</th><th>动作</th><th>工具</th><th>结果</th><th>耗时</th></tr></thead>
      <tbody>{step_rows or '<tr><td colspan="5" class="muted">无工具步骤</td></tr>'}</tbody>
    </table>
  </div>
</section>
<section class="panel">
  <div class="panel-head"><h2>计划与完整记录</h2><span class="muted">只读 JSON</span></div>
  <details><summary>结构化计划</summary>{_json_block(record.get("plan") or [])}</details>
  <details><summary>完整 Run Record</summary>{_json_block(record)}</details>
</section>
"""


def experiments_shell(page_key: str, title: str, subtitle: str, content: str) -> str:
    response = render_shell(
        title,
        page_key,
        subtitle,
        content,
        active_key="experiments",
    )
    return response.body.decode(response.charset)


__all__ = [
    "batch_page",
    "batches_page",
    "experiments_shell",
    "run_page",
]
