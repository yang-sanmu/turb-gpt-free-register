# -*- coding: utf-8 -*-
"""iCloud 邮箱来源（转发到 QQ 邮箱，复用 QQ_EMAIL / QQ_IMAP_PASSWORD 收码）。"""
from config.env_loader import apply_env_overrides

# 母邮箱列表，每行一个，例如 foam_grungy8c@icloud.com。
ICLOUD_EMAILS = []

# direct = 每个邮箱只用一次；plus = 循环使用母邮箱，每次注册追加 +7 位随机后缀。
# plus_once = 每个母邮箱只成功一次，首次领取生成固定 +随机后缀，失败回收后沿用。
# 同一母邮箱已有任意 +后缀账号则跳过；裸母邮箱账号不影响子邮箱领取。
ICLOUD_MODE = "plus"

# plus / plus_once 模式下新后缀长度（小写字母 + 数字），不影响已生成的固定后缀。
ICLOUD_PLUS_LENGTH = 7

apply_env_overrides(globals(), {
    "ICLOUD_EMAILS": "list_str_multiline",
    "ICLOUD_MODE": "str",
    "ICLOUD_PLUS_LENGTH": "int",
})
