from __future__ import annotations

import json
import pathlib
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


def _read_recent_logs(count: int = 20) -> list[dict[str, Any]]:
    """Read most recent audit log entries from today's log file."""
    log_dir = pathlib.Path("logs")
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"audit-{today}.jsonl"
    if not log_file.exists():
        return []
    entries = []
    for line in log_file.read_text(encoding="utf-8").strip().split("\n"):
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries[-count:]


HTML_PAGE = """
<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"UTF-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1.0\">
<title>MoA Gateway Dashboard</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,sans-serif; background:#f5f5f5; color:#333; padding:20px; }
h1 { font-size:24px; margin-bottom:20px; }
.card { background:#fff; border-radius:8px; padding:16px; margin-bottom:16px; box-shadow:0 1px 3px rgba(0,0,0,0.1); }
.card h2 { font-size:16px; margin-bottom:12px; color:#666; }
.status { display:inline-block; padding:4px 12px; border-radius:12px; font-size:13px; font-weight:600; }
.status.ok { background:#e6f7e6; color:#1a7d1a; }
.status.degraded { background:#fff3e0; color:#e65100; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; }
.stat { text-align:center; padding:16px; }
.stat .value { font-size:28px; font-weight:700; }
.stat .label { font-size:13px; color:#999; margin-top:4px; }
.log-entry { padding:8px 0; border-bottom:1px solid #eee; font-size:13px; }
.log-entry:last-child { border-bottom:none; }
.log-entry .time { color:#999; font-size:12px; }
.log-entry .path { font-weight:600; }
.log-entry .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; margin-left:8px; }
.badge-2xx { background:#e6f7e6; color:#1a7d1a; }
.badge-4xx { background:#fff3e0; color:#e65100; }
.badge-5xx { background:#fde8e8; color:#c62828; }
form { display:flex; flex-direction:column; gap:8px; }
textarea, input, select { padding:8px; border:1px solid #ddd; border-radius:4px; font-size:14px; }
button { padding:10px 20px; background:#1a73e8; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:14px; }
button:hover { background:#1557b0; }
pre { background:#f8f8f8; padding:12px; border-radius:4px; font-size:12px; overflow-x:auto; margin-top:8px; }
</style>
</head>
<body>
<h1>MoA Gateway Dashboard</h1>

<div class=\"card\">
<h2>System Status</h2>
<div id=\"health-status\">Loading...</div>
</div>

<div class=\"card\">
<h2>Statistics</h2>
<div class=\"grid\" id=\"stats\">
<div class=\"stat\"><div class=\"value\" id=\"total-req\">-</div><div class=\"label\">Total Requests</div></div>
<div class=\"stat\"><div class=\"value\" id=\"success-rate\">-</div><div class=\"label\">Success Rate</div></div>
<div class=\"stat\"><div class=\"value\" id=\"avg-duration\">-</div><div class=\"label\">Avg Duration (ms)</div></div>
</div>
</div>

<div class=\"card\">
<h2>Test Webhook</h2>
<form id=\"webhook-form\">
<input type=\"text\" id=\"wh-text\" value=\"hello\" placeholder=\"Input text\">
<button type=\"submit\">Send Test Request</button>
</form>
<pre id=\"wh-result\"></pre>
</div>

<div class=\"card\">
<h2>Recent Requests</h2>
<div id=\"log-entries\">{log_entries}</div>
</div>

<script>
fetch('/healthz').then(r=>r.json()).then(d=>{
  const ok = d.status === 'healthy';
  document.getElementById('health-status').innerHTML =
    '<span class=\"status ' + (ok ? 'ok' : 'degraded') + '\">' + d.status + '</span>'
    + '<pre>' + JSON.stringify(d.checks, null, 2) + '</pre>';
});

document.getElementById('webhook-form').onsubmit = async function(e) {
  e.preventDefault();
  const text = document.getElementById('wh-text').value;
  const body = JSON.stringify({session_id:'dash-test',user_id:'admin',text:text,message_id:'dash-'+Date.now()});
  const pre = document.getElementById('wh-result');
  pre.textContent = 'Sending...';
  try {
    const r = await fetch('/webhook/feishu', {method:'POST', headers:{'Content-Type':'application/json'}, body});
    const data = await r.text();
    pre.textContent = r.status + ' ' + r.statusText + '\\n' + JSON.stringify(JSON.parse(data), null, 2);
  } catch(e) { pre.textContent = 'Error: ' + e.message; }
};
</script>
</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    logs = _read_recent_logs(20)
    log_html = ""
    for entry in logs:
        ts = entry.get("timestamp", "")[:19]
        status = entry.get("extra", {}).get("status", 200)
        intent = entry.get("intent", "")
        badge_class = "badge-2xx" if status < 400 else "badge-4xx" if status < 500 else "badge-5xx"
        log_html += f'<div class="log-entry"><span class="time">{ts}</span> <span class="badge {badge_class}">{status}</span> <span class="path">{intent}</span></div>'

    if not logs:
        log_html = '<div class="log-entry" style="color:#999">No request logs yet. Send a webhook request first.</div>'

    return HTMLResponse(HTML_PAGE.replace("{log_entries}", log_html))
