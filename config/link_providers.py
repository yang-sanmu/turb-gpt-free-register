# -*- coding: utf-8 -*-
"""独立提链服务配置；cdk 表示使用上游 extract_link 的 CDK 提链服务。"""
from config.env_loader import apply_env_overrides

# 提链服务：cdk=上游 CDK 提链服务；nerver_upi=cha.nerver.cc UPI（免鉴权，传入代理）
LINK_PROVIDER = "cdk"

# Nerver UPI 接口地址；请求本身直连，不走注册代理池。
NERVER_UPI_API_BASE = "https://cha.nerver.cc"
# stream=流式进度 /api/v1/upi/stream；json=非流式结果 /api/v1/upi。
NERVER_UPI_MODE = "stream"
# 作为接口参数交给 Nerver 服务端使用的代理（公网可达，建议印度出口），每次随机取一条。
NERVER_UPI_PROXY_POOL = []
# 单次提链等待超时（秒），非流式建议至少 300 秒。
NERVER_UPI_TIMEOUT = 300

apply_env_overrides(globals(), {
    "LINK_PROVIDER": "str",
    "NERVER_UPI_API_BASE": "str",
    "NERVER_UPI_MODE": "str",
    "NERVER_UPI_PROXY_POOL": "list_str_multiline",
    "NERVER_UPI_TIMEOUT": "int",
})
