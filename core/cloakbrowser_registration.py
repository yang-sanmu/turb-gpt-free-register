# -*- coding: utf-8 -*-
"""共用 Playwright 适配层的注册流程，默认使用 CloakBrowser。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data, post_register_dwell
from core.browser_data_saver import BrowserDataSaver
from core.browser_traffic import PlaywrightTrafficTracker, merge_proxy_transport_traffic
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import acquire_email_after_input, wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
)

logger = logging.getLogger(__name__)


class _ProviderLogger(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return str(msg).replace("[Cloak注册]", f"[{self.extra['label']}注册]"), kwargs


def run_cloak_registration(
    email: str | None,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
    *,
    browser_factory=None,
    provider: str = "cloakbrowser",
    capture_state=None,
    network_factory=None,
    on_session_error=None,
) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    traffic_tracker = None
    data_saver: BrowserDataSaver | None = None
    network_traffic: dict | None = None
    label = "GptAccount" if provider == "gptaccount" else "Cloak"
    log = _ProviderLogger(logger, {"label": label})
    account_id = None
    credential_snapshot = {}
    result = None
    try:
        driver, opened = (browser_factory or build_cloak_driver)(proxy=proxy)
        if network_factory is not None:
            traffic_tracker, data_saver = network_factory(driver, label=label)
        else:
            try:
                traffic_tracker = PlaywrightTrafficTracker(driver.context, label=label)
            except Exception as exc:
                # 统计失败不应影响注册主流程。
                log.warning("[Cloak注册] 初始化浏览器流量统计失败，继续注册：%s: %s", type(exc).__name__, str(exc)[:180])
            data_saver = BrowserDataSaver(label=label)
            if traffic_tracker is not None:
                traffic_tracker.attach_data_saver(data_saver)
            data_saver.install_playwright(driver.context)
        log.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        log.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        driver.get("https://chatgpt.com/auth/login")
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()

        def _email_supplier_after_input() -> str:
            nonlocal email
            _check_manual_stop()
            email = acquire_email_after_input(email)
            if on_email_acquired:
                on_email_acquired(email)
            return email

        next_state = _submit_email_and_wait_next(
            driver,
            email,
            attempts=3,
            email_supplier=_email_supplier_after_input,
        )
        _check_manual_stop()

        # 如果邮箱提交后直接进入验证码页，也尝试点击“使用密码继续”进入密码创建页；
        # _fill_password_page_if_present 会在设置成功后返回本次 OpenAI 注册密码。
        openai_password = _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                log.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    _check_manual_stop()
                    if otp_attempt >= max_otp_attempts:
                        raise
                    log.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            _check_manual_stop()
            log.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                log.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            if outcome == "accepted":
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            otp_after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        try:
            session_info = _fetch_chatgpt_session(driver, timeout=120)
        except Exception as session_exc:
            if create_acknowledged and on_session_error is not None:
                try:
                    account_id = on_session_error(
                        email=email, name=name, password=openai_password, error=session_exc,
                        proxy=((opened.raw or {}).get("proxy_pool_target") if opened else None) or proxy or None,
                    )
                except Exception:
                    log.exception("[Cloak注册] session 失败回调异常：%s", email)
            raise
        access_token = session_info["accessToken"]
        if capture_state is not None:
            try:
                credential_snapshot["browser_session"] = capture_state(driver)
            except Exception as exc:
                # An account already exists. Persist its credentials even if the
                # browser disappeared while reading cookies; report partial success.
                credential_snapshot["browser_session_error"] = type(exc).__name__
        log.info("[Cloak注册] 已拿到 accessToken：%s", email)

        if _twofa_cfg.ENABLE_2FA:
            log.info("[Cloak注册] 2FA 将由现有后台任务设置，成功后回写账号")
        totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if provider == "gptaccount":
                codex_result["message"] = "GptAccount 第一阶段仅注册，Codex OAuth 暂不执行"
            elif bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                log.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                log.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        # 统计注册浏览器关闭前的完整会话；注册后停留期间的网络请求也计入。
        if provider != "gptaccount":
            post_register_dwell(email, label=f"{label}注册")
        if traffic_tracker is not None:
            network_traffic = traffic_tracker.stop()
        network_traffic = merge_proxy_transport_traffic(network_traffic, driver.proxy_transport_snapshot(), label=label)
        if data_saver is not None:
            data_saver.stop()
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy_pool_target") if opened else None) or proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                provider: {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
                "network_traffic": network_traffic,
                **credential_snapshot,
            },
        )
        if provider == "gptaccount":
            post_register_dwell(email, label=f"{label}注册")
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        snapshot_error = credential_snapshot.get("browser_session_error")
        result = {
            "success": bool(codex_ok) and not snapshot_error,
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "network_traffic": network_traffic,
            "error": (f"账号已保存，但 Cookies 快照失败：{snapshot_error}" if snapshot_error
                      else None if codex_ok else f"Codex 未完成: {codex_result.get('message')}"),
        }
        return result
    except Exception as exc:
        if traffic_tracker is not None:
            try:
                network_traffic = traffic_tracker.stop()
            except Exception:
                pass
        if driver is not None:
            network_traffic = merge_proxy_transport_traffic(network_traffic, driver.proxy_transport_snapshot(), label=label)
        if data_saver is not None:
            data_saver.stop()
        log.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        log.debug("[Cloak注册] 失败详情", exc_info=True)
        try:
            if email and account_id is None:
                from core.email_provider import release_email
                release_email(email, status="failed" if create_acknowledged else "available", note=f"{label}注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        result = {
            "success": False,
            "email": email,
            "account_id": account_id,
            "network_traffic": network_traffic,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
        return result
    finally:
        if traffic_tracker is not None:
            try:
                traffic_tracker.stop()
            except Exception:
                pass
        if data_saver is not None:
            try:
                data_saver.stop()
            except Exception:
                pass
        if driver and (provider == "gptaccount" or not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN)):
            try:
                driver.quit()
            except Exception as exc:
                if provider == "gptaccount":
                    log.error("[%s注册] 浏览器清理失败：%s", label, exc)
                    if result is not None:
                        result["success"] = False
                        result["error"] = f"{result.get('error') or ''} 浏览器清理未完成：{exc}".strip()
