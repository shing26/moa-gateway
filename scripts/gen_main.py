import pathlib, ast
q = chr(34)
path = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
old = path.read_text(encoding="utf-8-sig")

# 1: add loader import
old = old.replace("from app.agents.contract import AgentEnvelope, get_agent", "from app.agents.contract import AgentEnvelope, get_agent\nimport app.agents.loader")\\n\\n# 2: debug handler + hitl fix + name fix\\nold = old.replace(\\n    "app.add_middleware(FeatureFlagMiddleware, client=_flag_client)",
    "app.add_middleware(FeatureFlagMiddleware, client=_flag_client)\n\n@app.exception_handler(Exception)\nasync def _debug_exception_handler(request: Request, exc: Exception):\n    import traceback\n    tb = traceback.format_exception_type(type(exc), exc, exc.__traceback__)\n    logger.error("unhandled exception: %s", "".join(tb))\n    return JSONResponse(\n        status_code=500,\n        content={"error": type(exc).__name__, "detail": str(exc)[:500]},\n    )"
)

# 3: remove hitl block
i=old.find("hitl_event = MoAevent(");j=old.find("if _card_sender",i);old=old[:i]+old[j:]

# 4: fix agent_name resolution
old = old.replace(
    "agent_name = intent if agent else \"general\"",
    "agent_name = intent if agent else \"general\"\n        for name in (\"coder\", \"general\"):\n            if get_agent(name) is agent:\n                agent_name = name\n                break",
)

# 5: fix review state string
from string import Template
old = old.replace(
    "\\"state\\": session_state.context.state.value,\n                \"intent\": intent,\n                 \"status\": \"pending_review\"",
    "\\"state\\": \"SUSPENDED\",\n                 \"intent\": intent,\n                  \"status\": \"pending_review\"",
)

path.write_text(old, encoding="utf-8")
ast.parse(old)
print("OK")
