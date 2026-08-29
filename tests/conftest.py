"""全局测试环境配置。

必须在任何测试模块 import ``app.main`` **之前**注入鉴权密钥：``AuthMiddleware``
在 ``app.main`` 导入时就把令牌固化进中间件实例（app/main.py 装配段），
之后再改环境变量对中间件无效。pytest 会先加载本文件，因此这里是唯一可靠时机。

用 ``setdefault`` 而非直接赋值：本机已配置真实密钥时不覆盖，
测试凭据统一由 ``tests/support.py`` 从环境变量回读，二者不会脱节。
"""

from __future__ import annotations

import os

DEFAULT_GATEWAY_TOKEN = "test-gateway-token"  # nosec B105
DEFAULT_DASHBOARD_PASSWORD = "test-dashboard-pw"  # nosec B105

os.environ.setdefault("WEBHOOK_AUTH_TOKEN", DEFAULT_GATEWAY_TOKEN)
os.environ.setdefault("DASHBOARD_PASSWORD", DEFAULT_DASHBOARD_PASSWORD)
