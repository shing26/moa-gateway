import base64
q = chr(34)
n = chr(10)
lines = []
lines.append('import pathlib, ast')
lines.append('p = pathlib.Path(' + q + 'D:/HermesData/moa-gateway/app/main.py' + q + ')')
lines.append('c = p.read_text(encoding=' + q + 'utf-8-sig' + q + ')')
lines.append('q2 = chr(34)')
lines.append('i = c.find(' + q + 'from app.agents.contract import AgentEnvelope, get_agent' + q + ')')
lines.append('j = c.find(chr(10), i)')
lines.append('c = c[:j+1] + ' + q + 'import app.agents.loader' + q + ' + chr(10) + c[j+1:]')
lines.append('m = ' + q + 'app.add_middleware(FeatureFlagMiddleware, client=_flag_client)' + q + '')
lines.append('h = chr(10)+chr(10)+chr(64)+chr(97)+chr(112)+chr(112)+chr(46)+chr(101)+chr(120)+chr(99)+chr(101)+chr(112)+chr(116)+chr(105)+chr(111)+chr(110)+chr(95)+chr(104)+chr(97)+chr(110)+chr(100)+chr(108)+chr(101)+chr(114)+chr(40)+chr(69)+chr(120)+chr(99)+chr(101)+chr(112)+chr(116)+chr(105)+chr(111)+chr(110)+chr(41)+chr(10)')
h += chr(97)+chr(115)+chr(121)+chr(110)+chr(99)+chr(32)+chr(100)+chr(101)+chr(102)+chr(32)+chr(95)+chr(100)+chr(101)+chr(98)+chr(117)+chr(103)+chr(95)+chr(101)+chr(120)+chr(99)+chr(101)+chr(112)+chr(116)+chr(105)+chr(111)+chr(110)+chr(95)+chr(104)+chr(97)+chr(110)+chr(100)+chr(108)+chr(101)+chr(114)+chr(40)+chr(114)+chr(101)+chr(113)+chr(117)+chr(101)+chr(115)+chr(116)+chr(58)+chr(32)+chr(82)+chr(101)+chr(113)+chr(117)+chr(101)+chr(115)+chr(116)+chr(44)+chr(32)+chr(101)+chr(120)+chr(99)+chr(58)+chr(32)+chr(69)+chr(120)+chr(99)+chr(101)+chr(112)+chr(116)+chr(105)+chr(111)+chr(110)+chr(41)+chr(58)+chr(10)