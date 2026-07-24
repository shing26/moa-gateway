import pathlib,ast
p=pathlib.Path('D:/HermesData/moa-gateway/app/main.py')
c=p.read_text(encoding='utf-8-sig')
q=chr(34)
i=c.find('from app.agents.contract import AgentEnvelope, get_agent')
j=c.find(chr(10),i)
c=c[:j+1]+'import app.agents.loader'+chr(10)+c[j+1:]
m='app.add_middleware(FeatureFlagMiddleware,client=_flag_client)'
h=chr(10)+chr(10)+'@app.exception_handler(Exception)'+chr(10)j=c.find(chr(10),i)
c=c[:j+1]+'import app.agents.loader'+chr(10)+c[j+1:]
m='app.add_middleware(FeatureFlagMiddleware,client=_flag_client)'
h=chr(10)+chr(10)+'@app.exception_handler(Exception)'+chr(10)
h+='async def _debug_exception_handler(request:Request,exc:Exception):'+chr(10)
h+='    import traceback'+chr(10)
h+='    tb=traceback.format_exception(type(exc),exc,exc.__traceback__)'+chr(10)h+='async def _debug_exception_handler(request:Request,exc:Exception):'
h+='    import traceback'
h+='    tb=traceback.format_exception(type(exc),exc,exc.__traceback__)'
h+='    logger.error('+q+'unhandled exception: %s'+q+','+q+q+'.join(tb))'
h+='    return JSONResponse(status_code=500,content={'+q+'error'+q+:type(exc).__name__,'+q+'detail'+q+:str(exc)[:500]})'
c=c.replace(m,m+h)
i=c.find('hitl_event = MoAEvent(')
j=c.find('if _card_sender',i)
if i>0:c=c[:i]+c[j:]
o='agent_name = intent if agent else '+q+'general'+q
nl=[o,'        for name in ('+q+'coder'+q+','+q+'general'+q+'):','            if get_agent(name) is agent:','                agent_name=name','                break']
c=c.replace(o,chr(10).join(nl),1)
o2=q+'state'+q+:session_state.context.state.value,'+chr(10)+'                '+q+'intent'+q+:intent,'+chr(10)+'                '+q+'status'+q+:'+q+'pending_review'+q
n2=q+'state'+q+:'+q+'SUSPENDED'+q+,'
c=c.replace(o2,n2,1)
p.write_text(c,encoding='utf-8')
ast.parse(c)
print('OK')