# -*- coding: utf-8 -*-
"""iCloud 邮箱来源：母邮箱转发到 QQ 邮箱，复用 QQ IMAP 收码。

iCloud 转发后 To 头只剩母邮箱（不带 +后缀），但发件方的 VERP 退信地址
（Sender / X-QQ-CSender / smtp.mailfrom）会带上完整收件人，例如
``bounces+20216706-738d-foo.bar+kj59aaf=icloud.com``。plus 模式靠它区分
同一母邮箱下并行注册的验证码。
"""
from __future__ import annotations

import email as email_lib
import itertools
import logging
import random
import re
import string
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

from config import email as _email_cfg
from config import icloud as cfg
from core import db
from core.otp_utils import extract_otp, looks_like_openai_email
from core.qqmail_client import _connect_imap, _decode_email_header, _msg_to_dict, _parse_email_date

logger = logging.getLogger(__name__)

_TABLE = "icloud_email_usage"
_ROUND_ROBIN = itertools.count(random.randrange(1000))
_RECIPIENT_HEADERS = ("To", "Sender", "X-QQ-CSender", "Return-Path", "Authentication-Results", "Received-SPF")
_RECENT_MESSAGES = 40
# QQ 会把部分 iCloud 转发的 OpenAI 邮件判为垃圾邮件（IMAP 目录名 Junk）。
_FOLDERS = ("INBOX", "Junk")


class ICloudMailError(RuntimeError):
    pass


def _conn():
    conn = db._sqlite_conn()
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} (email TEXT PRIMARY KEY COLLATE NOCASE, "
        "base TEXT, status TEXT, note TEXT, updated_at TEXT)"
    )
    return conn


def _bases() -> list[str]:
    out = []
    for raw in cfg.ICLOUD_EMAILS or []:
        local, _, domain = str(raw or "").strip().lower().partition("@")
        addr = f"{local.split('+', 1)[0]}@{domain}"  # 母邮箱不带 +后缀
        if local and domain and addr not in out:
            out.append(addr)
    if not out:
        raise ICloudMailError("ICLOUD_EMAILS 为空，请在 配置 → 邮箱 / OTP 填写 iCloud 母邮箱（每行一个）")
    return out


def _record(conn, email: str, base: str, status: str, note: str | None = None) -> None:
    conn.execute(
        f"INSERT INTO {_TABLE}(email, base, status, note, updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(email) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at",
        (email, base, status, note, datetime.now().isoformat(timespec="seconds")),
    )


def pick_email() -> str:
    mode = str(cfg.ICLOUD_MODE or "plus").strip().lower()
    if mode not in ("direct", "plus"):
        raise ICloudMailError(f"ICLOUD_MODE={mode!r} 无效，可选 direct / plus")
    bases = _bases()
    with db._LOCK, closing(_conn()) as conn, conn:
        if mode == "direct":
            taken = {r[0].lower() for r in conn.execute(f"SELECT email FROM {_TABLE} WHERE status != 'available'")}
            for base in bases:
                if base not in taken and db.get_account_by_email(base) is None:
                    _record(conn, base, base, "used")
                    logger.info("[iCloud] 领取邮箱: %s", base)
                    return base
            raise ICloudMailError(f"ICLOUD_EMAILS 中 {len(bases)} 个邮箱均已使用，请补充或切换 plus 模式")

        length = max(4, min(16, int(cfg.ICLOUD_PLUS_LENGTH or 7)))
        base = bases[next(_ROUND_ROBIN) % len(bases)]
        local, domain = base.split("@", 1)
        for _ in range(20):
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
            addr = f"{local}+{suffix}@{domain}"
            if conn.execute(f"SELECT 1 FROM {_TABLE} WHERE email=?", (addr,)).fetchone() is None:
                _record(conn, addr, base, "used")
                logger.info("[iCloud] 生成子邮箱: %s", addr)
                return addr
    raise ICloudMailError("生成 iCloud 子邮箱失败：随机后缀连续冲突")


def is_known_email(email: str) -> bool:
    try:
        with closing(_conn()) as conn:
            return conn.execute(f"SELECT 1 FROM {_TABLE} WHERE email=?", (str(email or "").strip(),)).fetchone() is not None
    except Exception:
        return False


def release_email(email: str, status: str = "available", note: str | None = None) -> None:
    email = str(email or "").strip().lower()
    with db._LOCK, closing(_conn()) as conn, conn:
        row = conn.execute(f"SELECT base FROM {_TABLE} WHERE email=?", (email,)).fetchone()
        _record(conn, email, row[0] if row else email.split("+", 1)[0], status, note)


def is_addressed_to(header_text: str, to_text: str, email: str) -> bool:
    """判断邮件是否发给 email。header_text 为收件相关头拼接，to_text 为 To 头。"""
    flat = re.sub(r"\s+", "", str(header_text or "")).lower()
    local, _, domain = str(email or "").strip().lower().partition("@")
    verp = re.compile(r"(?<![a-z0-9._+])" + re.escape(local) + "=" + re.escape(domain))
    if verp.search(flat):
        return True
    if "+" in local:
        # 子邮箱只认退信地址；To 头只有母邮箱，按 To 匹配会和同母邮箱的并行任务串码。
        return False
    # direct：To 是母邮箱，但若退信地址显示是某个 +子邮箱，就属于 plus 任务。
    return f"{local}@{domain}" in str(to_text or "").lower() and f"{local}+" not in flat


def _recent_headers(mail, after_dt: datetime) -> list[tuple[bytes, email_lib.message.Message]]:
    status, data = mail.search(None, f"(SINCE {after_dt.strftime('%d-%b-%Y')})")
    ids = (data[0].split() if status == "OK" and data and data[0] else [])[-_RECENT_MESSAGES:]
    if not ids:
        return []
    status, data = mail.fetch(b",".join(ids), "(BODY.PEEK[HEADER])")
    out = []
    for part in data or []:
        if isinstance(part, tuple) and len(part) == 2:
            mid = part[0].split()[0]
            out.append((mid, email_lib.message_from_bytes(part[1])))
    return out


def _latest_otp_in_folder(mail, email: str, after_ts: float, after_dt: datetime) -> tuple[str, float, str] | None:
    """当前已选目录里发给 email 的最新 OpenAI 验证码：(otp, ts, subject)。"""
    for mid, head in sorted(_recent_headers(mail, after_dt), key=lambda x: _parse_email_date(x[1]) or 0, reverse=True):
        ts = _parse_email_date(head) or 0.0
        if ts and ts < after_ts - 30:
            continue
        header_text = " ".join(_decode_email_header(v) for k in _RECIPIENT_HEADERS for v in (head.get_all(k) or []))
        if not is_addressed_to(header_text, _decode_email_header(head.get("To") or ""), email):
            continue
        status, data = mail.fetch(mid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            continue
        item = _msg_to_dict(email_lib.message_from_bytes(data[0][1]))
        otp = extract_otp(item) if looks_like_openai_email(item) else None
        if otp:
            return otp, ts, str(item.get("subject") or "")
    return None


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    after_ts = float(after_ts or time.time())
    max_wait = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS)
    after_dt = datetime.fromtimestamp(after_ts - 30, tz=timezone.utc)
    deadline = time.time() + max_wait
    best_otp, best_ts, settle_until = None, 0.0, None
    logger.info("[iCloud] 经 QQ 邮箱轮询验证码：%s，最长 %ss", email, max_wait)

    while time.time() < deadline:
        mail = None
        try:
            mail = _connect_imap()  # 已 select INBOX
            for folder in _FOLDERS:
                if folder != "INBOX" and mail.select(folder, readonly=True)[0] != "OK":
                    continue
                found = _latest_otp_in_folder(mail, email, after_ts, after_dt)
                if found and found[1] > best_ts:
                    otp, ts, subject = found
                    if otp != best_otp:
                        logger.info("[iCloud] 锁定验证码 %s（%s，subject=%r），等待 %ss settle", otp, folder, subject, settle)
                        best_otp, settle_until = otp, time.time() + settle
                    best_ts = ts
        except Exception as exc:
            logger.warning("[iCloud] QQ 邮箱取件失败：%s: %s", type(exc).__name__, exc)
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
        if best_otp and time.time() >= (settle_until or 0):
            return best_otp
        time.sleep(min(interval, max(0.5, (settle_until or deadline) - time.time())))

    if best_otp:
        return best_otp
    raise ICloudMailError(f"等待 iCloud 验证码超时（{max_wait}s）：{email}")
