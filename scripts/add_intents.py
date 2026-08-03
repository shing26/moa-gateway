import pathlib
TARGET = pathlib.Path(__file__).resolve().parents[1] / "app" / "router" / "intent_router.py"

with open(TARGET, 'r', encoding='utf-8-sig') as f:
    c = f.read()

old = '''self._regex_map = [
            (re.compile(r\"^(hi|hello|hey|你好|您好)$\", re.IGNORECASE), \"greeting\"),
            (re.compile(r\"(debug|错误|报错|traceback|exception)\", re.IGNORECASE), \"debug\"),
            (re.compile(r\"(code|代码|函数|class|模块|实现|refactor)\", re.IGNORECASE), \"coding\"),
            (re.compile(r\"(cancel|取消|重置|reset|stop)\", re.IGNORECASE), \"control\"),
        ]'''

new = '''self._regex_map = [
            (re.compile(r\"^(hi|hello|hey|你好|您好)$\", re.IGNORECASE), \"greeting\"),
            (re.compile(r\"(debug|错误|报错|traceback|exception)\", re.IGNORECASE), \"debug\"),
            (re.compile(r\"(code|代码|函数|class|模块|实现|refactor)\", re.IGNORECASE), \"coding\"),
            (re.compile(r\"(cancel|取消|重置|reset|stop)\", re.IGNORECASE), \"control\"),
            (re.compile(r\"(翻译|translate|英文|中文|english)\", re.IGNORECASE), \"translate\"),
            (re.compile(r\"(总结|摘要|summarize|概括|提炼)\", re.IGNORECASE), \"summarize\"),
            (re.compile(r\"(搜索|search|查找|查询|find)\", re.IGNORECASE), \"search\"),
            (re.compile(r\"(分析|analyze|统计|compare|对比|比较)\", re.IGNORECASE), \"analyze\"),
        ]'''

c = c.replace(old, new, 1)

with open(TARGET, 'w', encoding='utf-8') as f:
    f.write(c)

import ast
ast.parse(c)
print("OK")
