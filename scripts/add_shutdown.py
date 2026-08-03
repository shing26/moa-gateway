import pathlib
MAIN = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"

with open(MAIN, 'r', encoding='utf-8-sig') as f:
    c = f.read()

shutdown = '''

@app.on_event("shutdown")
async def _shutdown() -> None:
    logger.info("moa gateway shutting down")
    engine._pending_hitl.clear()
    _flag_client.invalidate()
'''

marker = 'tracer = trace.get_tracer("moa-gateway")'
idx = c.rfind(marker)
if idx > 0:
    idx = c.find('\n', idx)
    c = c[:idx+1] + shutdown + c[idx+1:]

with open(MAIN, 'w', encoding='utf-8') as f:
    f.write(c)

import ast
ast.parse(c)
print("OK")
