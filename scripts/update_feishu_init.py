with open('D:/HermesData/moa-gateway/app/main.py', 'r', encoding='utf-8-sig') as f:
    c = f.read()

c = c.replace(
    'from app.channels.feishu import FeishuChannelAdapter, FeishuConfig',
    'from app.channels.feishu import FeishuChannelAdapter, FeishuConfig\nfrom app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider'
)

old = (
    '    global _feishu_config, _card_sender\n'
    '    app_id = os.environ.get("FEISHU_APP_ID", "")\n'
    '    app_secret = os.environ.get("FEISHU_APP_SECRET", "")\n'
    '    if app_id and app_secret:\n'
    '        _feishu_config = FeishuConfig(app_id=app_id, app_secret=app_secret)\n'
    '        _card_sender = FeishuCardSender(_feishu_config)\n'
    '        logger.info("feishu card sender initialized")'
)

new = (
    '    global _feishu_config, _card_sender\n'
    '    app_id = os.environ.get("FEISHU_APP_ID", "")\n'
    '    app_secret = os.environ.get("FEISHU_APP_SECRET", "")\n'
    '    if app_id and app_secret:\n'
    '        _feishu_config = FeishuConfig(app_id=app_id, app_secret=app_secret)\n'
    '        auth_provider = FeishuTokenProvider(FeishuAuthConfig(app_id=app_id, app_secret=app_secret))\n'
    '        _card_sender = FeishuCardSender(auth_provider)\n'
    '        logger.info("feishu card sender initialized")'
)

c = c.replace(old, new, 1)

with open('D:/HermesData/moa-gateway/app/main.py', 'w', encoding='utf-8') as f:
    f.write(c)

import ast
ast.parse(c)
print("OK")
