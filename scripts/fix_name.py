import re,pathlib
MAIN=pathlib.Path(__file__).resolve().parents[1]/'app'/'main.py'
with open(MAIN,encoding='utf-8-sig') as f:c=f.read()
old_line='agent_name = intent if agent else \"general\"'
new_lines=[old_line]
new_lines.append('for name in (\"coder\", \"general\"):')
new_lines.append('        if get_agent(name) is agent:')
new_lines.append('            agent_name = name')
new_lines.append('            break')
c=c.replace(old_line,'\n'.join(new_lines))
with open(MAIN,'w',encoding='utf-8') as f:f.write(c)
import ast;ast.parse(c);print('OK')
