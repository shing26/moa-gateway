import pathlib, tempfile, time
r = []
def card(t, s, c, f, p, sol, b, bef, aft):
    r.append(f'<div class="bg-white rounded-lg shadow-md p-6 mb-6"><div class="flex justify-between items-start mb-4"><h2 class="text-xl font-semibold">{t}</h2><span class="{c} px-3 py-1 rounded-full text-sm font-medium">{s}</span></div><p class="text-sm text-gray-500 mb-2">Files: {f}</p><p class="text-gray-700 mb-2"><strong>Problem:</strong> {p}</p><p class="text-gray-700 mb-2"><strong>Solution:</strong> {sol}</p><p class="text-gray-700 mb-4"><strong>Benefit:</strong> {b}</p><div class="grid grid-cols-2 gap-4"><div class="p-3 bg-red-50 rounded"><p class="text-sm font-medium text-red-800">Before</p><p class="text-sm text-red-600">{bef}</p></div><div class="p-3 bg-green-50 rounded"><p class="text-sm font-medium text-green-800">After</p><p class="text-sm text-green-600">{aft}</p></div></div></div>')

card("Duplicate Feishu Auth", "Worth exploring", "bg-yellow-100 text-yellow-800",
     "feishu.py, feishu_cards.py",
     "Both files implement the same Feishu OAuth token acquisition.",
     "Extract a shared FeishuTokenProvider class.",
     "One place to fix when Feishu API changes.",
     "20 lines duplicated: _tenant_access_token and _ensure_token.",
     "Single FeishuTokenProvider, new integrations get auth for free.")

card("Duplicated Agent Execute", "Strong", "bg-green-100 text-green-800",
     "app/agents/stubs.py",
     "CoderAgent.execute and GeneralAgent.execute are 90% identical.",
     "Extract shared logic into a base class or helper function.",
     "Adding a third agent becomes a one-liner.",
     "Two nearly identical execute methods with copy-pasted message building.",
     "Single _execute helper. New agents provide role tag + instructions.")

card("main.py God Module", "Worth exploring", "bg-blue-100 text-blue-800",
     "app/main.py (53 statements, 7 routes)",
     "Mixes app wiring with business logic. Webhook, callback, privacy, shutdown all in one file.",
     "Split into main.py, routes/webhook.py, routes/privacy.py, lifecycle.py.",
     "Each router independently testable via TestClient.",
     "Single file handles FastAPI setup, 7 routes, startup, shutdown.",
     "Clean separation: wiring, routes, lifecycle in separate modules.")

card("Dead Code: span()", "Strong", "bg-green-100 text-green-800",
     "app/observability/tracing.py",
     "span() async context manager is defined but never imported anywhere.",
     "Either remove span() or replace all raw tracer calls with it.",
     "Dead code elimination or a single tested tracing wrapper.",
     "span() defined with full implementation, zero call sites.",
     "Either removed, or adopted as the single tracing seam.")

card("Two Guard Impls", "Speculative", "bg-purple-100 text-purple-800",
     "guard_service.py, permission_guard.py",
     "GuardService + FailClosedPermissionGuard overlap. Both called in sequence.",
     "Fold permission guard into GuardService.",
     "Single guard seam to test.",
     "Two guards called sequentially: evaluate then check.",
     "GuardService handles everything. permission_guard.py removed.")

html = "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1.0\"><script src=\"https://cdn.tailwindcss.com\"></script><title>MoA Gateway Architecture Review</title></head><body class=\"bg-gray-50 p-8\"><div class=\"max-w-5xl mx-auto\"><h1 class=\"text-3xl font-bold mb-2\">MoA Gateway Architecture Review</h1><p class=\"text-gray-500 mb-6\">Generated " + time.strftime("%Y-%m-%d %H:%M") + "</p>" + "".join(r) + "<div class=\"bg-indigo-50 border-l-4 border-indigo-500 rounded-lg p-6\"><h2 class=\"text-xl font-semibold text-indigo-800 mb-2\">Top Recommendation</h2><p class=\"text-gray-700\"><strong>#1 Duplicated Agent Execute</strong> - Highest impact, lowest risk.</p><p class=\"text-gray-700 mt-2\"><strong>#2 Dead Code: span()</strong> - Remove or adopt.</p></div></div></body></html>"

p = pathlib.Path(tempfile.gettempdir()) / f"architecture-review-{int(time.time())}.html"
p.write_text(html, encoding="utf-8")
print(f"Report: {p}")
