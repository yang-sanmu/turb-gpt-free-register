# -*- coding: utf-8 -*-
"""iCloud 邮箱来源（转发到 QQ 邮箱，复用 QQ_EMAIL / QQ_IMAP_PASSWORD 收码）。"""
from config.env_loader import apply_env_overrides

# 母邮箱列表，每行一个，例如 foam_grungy8c@icloud.com。
ICLOUD_EMAILS = []

# direct = 每个邮箱只用一次；plus = 循环使用母邮箱，每次注册追加 +7 位随机后缀。
ICLOUD_MODE = "plus"

# plus 模式下后缀长度（与域名邮箱随机前缀同字符集：小写字母 + 数字）。
ICLOUD_PLUS_LENGTH = 7

apply_env_overrides(globals(), {
    "ICLOUD_EMAILS": "list_str_multiline",
    "ICLOUD_MODE": "str",
    "ICLOUD_PLUS_LENGTH": "int",
})
