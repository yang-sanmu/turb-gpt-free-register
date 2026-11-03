# -*- coding: utf-8 -*-
"""资料页提交后拿 AT 超时的补救，避免浪费只能注册一次的子邮箱。

超时时账号可能已经建好（只是 OAuth 回调被跳过），也可能资料没被服务端收下。
补救顺序：
1. 浏览器里记录到 create_account 返回 200：打开它的 continue_url 完成回调。
2. 浏览器回到 about-you 仍是资料页：在当前页面重新提交资料。
3. 协议重新登录：完整账号能拿到 Token；落到 about-you 说明子邮箱已被占用。
都判断不了（网络失败等）时，无 Token 入库标“待补取”，交给查活处理。
"""
from __future__ import annotations

import logging
import random
import time

from core import db
from core.email_provider import resolve_email_source

logger = logging.getLogger(__name__)

CREATE_ACCOUNT_PATH = "/api/accounts/create_account"
_PROBE_ATTR = "create_account_responses"


class SubAddressBurned(RuntimeError):
    """子邮箱已注册但资料未完成，服务端不再接受这个邮箱。"""


def attach_create_account_probe(driver) -> list[dict]:
    """在浏览器上下文记录 create_account 的状态码；响应体在需要时再读取。

    同步版 Playwright 的事件回调里不能调用 response.json() 等阻塞 API，
    否则会卡住事件循环。这里只保存 response 对象，补救时在主线程读取。
    """
    records: list[dict] = []
    setattr(driver, _PROBE_ATTR, records)

    def on_response(response):
        try:
            if CREATE_ACCOUNT_PATH not in str(response.url) or response.request.method != "POST":
                return
            records.append({"status": int(response.status), "response": response})
        except Exception:
            logger.debug("[注册补救] 记录 create_account 响应失败", exc_info=True)

    try:
        driver.context.on("response", on_response)
    except Exception as exc:
        logger.warning("[注册补救] 无法监听 create_account 响应，超时后仅靠重新登录判断：%s", exc)
    return records


def _accepted_continue_url(driver) -> str:
    """最后一次 200 的 create_account 响应里的 continue_url；读不到返回空串。"""
    for record in reversed(getattr(driver, _PROBE_ATTR, None) or []):
        if record.get("status") != 200:
            continue
        url = record.get("continue_url")
        if url is None:
            try:
                body = record["response"].json()
            except Exception:
                body = {}
            url = str(body.get("continue_url") or "") if isinstance(body, dict) else ""
            record["continue_url"] = url
        if url:
            return url
    return ""


def _absolute_auth_url(url: str) -> str:
    return "https://auth.openai.com" + url if url.startswith("/") else url


def _session_via_create_account_continue(driver) -> dict | None:
    from core.roxy_registration import _fetch_chatgpt_session

    continue_url = _accepted_continue_url(driver)
    if not continue_url:
        return None
    logger.info("[注册补救] create_account 已成功，打开 continue_url 补走 OAuth 回调")
    driver.get(_absolute_auth_url(continue_url))
    return _fetch_chatgpt_session(driver, timeout=60)


def _session_via_profile_resubmit(driver, name: str, birthday: str) -> dict | None:
    from core.roxy_registration import (
        _complete_profile_page, _fetch_chatgpt_session, _is_profile_like, _page_snapshot,
    )

    if not _is_profile_like(_page_snapshot(driver)):
        # 只有页面确实停在资料页时才重提；不主动导航，避免打断进行中的跳转。
        return None
    for attempt in range(1, 3):
        logger.info("[注册补救] 仍在资料页，重新提交资料（%s/2）", attempt)
        _complete_profile_page(driver, name, birthday, timeout=30)
        try:
            return _fetch_chatgpt_session(driver, timeout=60)
        except Exception as exc:
            logger.warning("[注册补救] 重新提交后仍未拿到 Token：%s", str(exc)[:200])
    return None


def _protocol_login_once(email: str, password: str | None, proxy: str | None) -> dict:
    from core.account_liveness import (
        _PASSWORD_STEP_FALLBACK_CODES, _exception_response_text, _extract_continue_url,
        _follow_continue_and_fetch, _network_preflight_with_retry, _password_verify, _validate_with_retry,
    )
    from core.openai_auth import follow_authorize, send_email_otp
    from core.session import close_browser_session

    session, authorize_url = _network_preflight_with_retry(email, proxy, fingerprint_state={})
    try:
        follow_authorize(session, authorize_url)
        otp_after_ts = time.time()
        result = None
        if password:
            try:
                result = _password_verify(session, password)
            except Exception as exc:
                if not any(code in _exception_response_text(exc) for code in _PASSWORD_STEP_FALLBACK_CODES):
                    raise
        page = (result or {}).get("page") or {}
        page_type = str(page.get("type") or "") if isinstance(page, dict) else ""
        continue_url = _extract_continue_url(result)
        if result is None or "email-verification" in continue_url or "email_otp" in page_type:
            if result is None:
                # 未走密码时服务端不会自动发码。
                send_email_otp(session)
                otp_after_ts = time.time()
            result = _validate_with_retry(session, email, otp_after_ts, email_source=resolve_email_source(email))
            page = result.get("page") or {}
            page_type = str(page.get("type") or "") if isinstance(page, dict) else ""
            continue_url = _extract_continue_url(result)
        if "about-you" in continue_url or page_type in {"about_you", "about-you"}:
            raise SubAddressBurned("重新登录后仍进入资料页，子邮箱已被占用")
        if "/mfa-challenge/" in continue_url or not continue_url:
            raise RuntimeError(f"重新登录未得到可用回调：page_type={page_type} continue_url={continue_url[:120]}")
        info = _follow_continue_and_fetch(session, continue_url, referer="https://auth.openai.com/email-verification")
        if not (info or {}).get("accessToken"):
            raise RuntimeError("重新登录回调完成但 session 没有 accessToken")
        return info
    finally:
        try:
            close_browser_session(session)
        except Exception:
            pass


def _session_via_protocol_login(email: str, password: str | None) -> dict:
    from config import proxy as proxy_cfg
    from core.account_liveness import _is_retryable_network_error
    from core.chatgpt_plan import open_plan_check_proxy, resolve_plan_check_route
    from core.live_check_service import _configured_live_check_proxies, _live_proxy_route

    candidates = _configured_live_check_proxies(proxy_cfg)
    routes = [_live_proxy_route(p, proxy_cfg) for p in random.sample(candidates, min(2, len(candidates)))]
    last_exc: Exception | None = None
    for route in routes or [resolve_plan_check_route()]:
        selected = str(route.get("proxy") or "")
        effective, relay = open_plan_check_proxy(route, selected, timeout=15.0)
        try:
            logger.info("[注册补救] 协议重新登录确认账号状态：%s", email)
            return _protocol_login_once(email, password, effective)
        except SubAddressBurned:
            raise
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_network_error(exc):
                raise
            logger.warning("[注册补救] 重新登录网络失败，换代理重试：%s", str(exc)[:200])
        finally:
            if relay is not None:
                try:
                    relay.close()
                except Exception:
                    pass
    raise last_exc or RuntimeError("重新登录未执行")


def _save_pending_account(*, email, name, password, error, proxy) -> int:
    existing = db.get_account_by_email(email)
    if existing is not None:
        return int(existing["id"])
    account_id = db.insert_account(
        email=email,
        access_token="",
        user_name=name,
        email_source=resolve_email_source(email),
        proxy_used=proxy,
        extra={"registration_password": password, "registration_session_error": str(error)[:500]},
    )
    db.update_account_note(
        account_id,
        f"已创建，注册时获取 Token 失败，补救时无法确认账号状态；待查活补取。原始错误：{str(error)[:300]}",
    )
    logger.warning("[注册补救] 无法确认账号状态，无 Token 入库待补取：id=%s email=%s", account_id, email)
    return account_id


def _is_stop_request(exc: BaseException) -> bool:
    try:
        from core.registration_service import StopRequested
    except ImportError:
        return False
    return isinstance(exc, StopRequested)


def recover_session_after_timeout(*, driver, email, name, birthday, password, error, proxy) -> dict:
    """返回 {"session_info": ...} 继续成功流程，{"account_id": id} 待补取，{} 表示子邮箱已作废。

    用户手动停止时也先把账号无 Token 入库：资料已提交，账号可能已建好，
    不能让子邮箱在没有任何记录的情况下流失。
    """
    from core.roxy_registration import _check_manual_stop

    def pending():
        return {"account_id": _save_pending_account(email=email, name=name, password=password, error=error, proxy=proxy)}

    steps = (
        ("continue_url", lambda: _session_via_create_account_continue(driver)),
        ("resubmit", lambda: _session_via_profile_resubmit(driver, name, birthday)),
        ("relogin", lambda: _session_via_protocol_login(email, password)),
    )
    for label, step in steps:
        try:
            _check_manual_stop()
            info = step()
        except SubAddressBurned as exc:
            logger.warning("[注册补救] %s：%s", exc, email)
            return {}
        except Exception as exc:
            if _is_stop_request(exc):
                logger.info("[注册补救] 用户停止任务，账号先无 Token 入库：%s", email)
                return pending()
            logger.warning("[注册补救] %s 步骤失败：%s: %s", label, type(exc).__name__, str(exc)[:200])
            continue
        if info and info.get("accessToken"):
            logger.info("[注册补救] 通过 %s 补回 Token：%s", label, email)
            return {"session_info": info}
    return pending()
