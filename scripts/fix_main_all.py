import re,ast,pathlib
MAIN=pathlib.Path(__file__).resolve().parents[1]/'app'/'main.py'
with open(MAIN,encoding='utf-8-sig') as f:c=f.read()

# 1. Add loader import
c=c.replace('from app.agents.contract import AgentEnvelope, get_agent','from app.agents.contract import AgentEnvelope, get_agent
import app.agents.loader')

# 2. Add debug exception handler
c=c.replace('app.add_middleware(FeatureFlagMiddleware, client=_flag_client)','app.add_middleware(FeatureFlagMiddleware, client=_flag_client)

@app.exception_handler(Exception)
async def _debug_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exception_type(type(exc), exc, exc.__traceback__)
    logger.error("unhandled exception: %s", "".join(tb))
    return JSONResponse(
        status_code=500,
        content={"error": type(exc).__name__, "detail": str(exc)[:500]},
    )
')

with open(MAIN,'w',encoding='utf-8') as f:f.write(c)
ast.parse(c);print('OK')
