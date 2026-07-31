from __future__ import annotations

import json
import pathlib
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.deps import memory, command_mode, knowledge_base, _retriever, _flag_client

router = APIRouter()


def _read_recent_logs(count: int = 30) -> list[dict[str, Any]]:
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


class UploadDoc(BaseModel):
    title: str
    content: str


@router.post("/dashboard/upload")
async def dashboard_upload(req: UploadDoc) -> JSONResponse:
    doc_id = await knowledge_base.add_document(req.title, req.content)
    return JSONResponse({"id": doc_id, "status": "ok"})


@router.post("/dashboard/delete")
async def dashboard_delete(body: dict) -> JSONResponse:
    doc_id = body.get("doc_id", "")
    ok = await knowledge_base.delete_doc(doc_id)
    return JSONResponse({"ok": ok})


@router.get("/dashboard/api/sessions")
async def dashboard_sessions() -> JSONResponse:
    sessions = []
    seen = set()
    for sid in list(memory._store.keys()):
        seen.add(sid)
        history = memory.get_history(sid, limit=3)
        mode = command_mode.get(sid) or "default"
        sessions.append({
            "session_id": sid[:16],
            "mode": mode,
            "history": [{"role": h["role"], "content": h["content"][:80]} for h in history],
        })
    for sid in list(command_mode._store.keys()):
        if sid not in seen:
            sessions.append({"session_id": sid[:16], "mode": command_mode.get(sid), "history": []})
    return JSONResponse({"sessions": sessions})


HTML_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
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
button.danger { background:#c62828; }
button.danger:hover { background:#a02020; }
pre { background:#f8f8f8; padding:12px; border-radius:4px; font-size:12px; overflow-x:auto; margin-top:8px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { padding:8px; text-align:left; border-bottom:1px solid #eee; }
th { color:#666; font-weight:600; }
.session-item { padding:10px 0; border-bottom:1px solid #eee; }
.session-item .sid { font-weight:600; font-size:13px; }
.session-item .mode { display:inline-block; margin-left:8px; padding:2px 8px; border-radius:10px; background:#e8eaf6; color:#3949ab; font-size:11px; }
.session-item .msg { font-size:12px; color:#666; margin-top:4px; }
</style>
</head>
<body>
<h1>MoA Gateway Dashboard</h1>

<div class="card">
<h2>System Status</h2>
<div id="health-status">Loading...</div>
</div>

<div class="card">
<h2>Knowledge Base</h2>
<div id="kb-list">Loading...</div>
<form id="kb-upload">
<input type="text" id="kb-title" placeholder="文档标题" required>
<textarea id="kb-content" placeholder="文档内容" rows="3" required></textarea>
<button type="submit">上传文档</button>
</form>
</div>

<div class="card">
<h2>Active Sessions</h2>
<div id="session-list">Loading...</div>
</div>

<div class="card">
<h2>Test Webhook</h2>
<form id="webhook-form">
<input type="text" id="wh-text" value="hello" placeholder="Input text">
<button type="submit">Send Test Request</button>
</form>
<pre id="wh-result"></pre>
</div>

<div class="card">
<h2>Recent Requests</h2>
<div id="log-entries">{log_entries}</div>
</div>

<script>
// Health
fetch('/healthz').then(r=>r.json()).then(d=>{
  const ok = d.status === 'healthy';
  document.getElementById('health-status').innerHTML =
    '<span class="status ' + (ok ? 'ok' : 'degraded') + '">' + d.status + '</span>'
    + '<pre>' + JSON.stringify(d.checks, null, 2) + '</pre>';
});

// Knowledge base
async function loadKb() {
  const r = await fetch('/knowledge/list');
  const d = await r.json();
  if (!d.documents || d.documents.length === 0) {
    document.getElementById('kb-list').innerHTML = '<div style="color:#999">暂无文档</div>';
    return;
  }
  let html = '<table><tr><th>ID</th><th>标题</th><th>分块</th><th></th></tr>';
  for (const doc of d.documents) {
    html += '<tr><td>' + doc.id.slice(0,8) + '</td><td>' + doc.title + '</td><td>' + doc.chunks + '</td>'
      + '<td><button class="danger" onclick="deleteDoc(\\'' + doc.id + '\\')">删除</button></td></tr>';
  }
  html += '</table>';
  document.getElementById('kb-list').innerHTML = html;
}

async function deleteDoc(id) {
  await fetch('/dashboard/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({doc_id:id})});
  loadKb();
}

document.getElementById('kb-upload').onsubmit = async function(e) {
  e.preventDefault();
  const body = JSON.stringify({title:document.getElementById('kb-title').value, content:document.getElementById('kb-content').value});
  await fetch('/dashboard/upload', {method:'POST', headers:{'Content-Type':'application/json'}, body});
  document.getElementById('kb-title').value = '';
  document.getElementById('kb-content').value = '';
  loadKb();
};

// Sessions
async function loadSessions() {
  const r = await fetch('/dashboard/api/sessions');
  const d = await r.json();
  if (!d.sessions || d.sessions.length === 0) {
    document.getElementById('session-list').innerHTML = '<div style="color:#999">暂无活跃会话</div>';
    return;
  }
  let html = '';
  for (const s of d.sessions) {
    html += '<div class="session-item"><span class="sid">' + s.session_id + '</span>'
      + '<span class="mode">' + (s.mode || 'default') + '</span>';
    for (const h of s.history) {
      html += '<div class="msg">[' + h.role + '] ' + h.content + '</div>';
    }
    html += '</div>';
  }
  document.getElementById('session-list').innerHTML = html;
}

// Webhook test
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

loadKb();
loadSessions();
</script>
</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    logs = _read_recent_logs(30)
    log_html = ""
    for entry in logs:
        ts = entry.get("timestamp", "")[:19]
        status = entry.get("extra", {}).get("status", 200)
        intent = entry.get("intent", "")
        badge_class = "badge-2xx" if status < 400 else "badge-4xx" if status < 500 else "badge-5xx"
        log_html += f'<div class="log-entry"><span class="time">{ts}</span> <span class="badge {badge_class}">{status}</span> <span class="path">{intent}</span></div>'
    if not logs:
        log_html = '<div class="log-entry" style="color:#999">No request logs yet.</div>'
    return HTMLResponse(HTML_PAGE.replace("{log_entries}", log_html))
