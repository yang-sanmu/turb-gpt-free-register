"""Use the maintained upstream registration steps with GptAccount Chrome."""
import logging
from datetime import datetime, timezone

from core import db
from core.cloakbrowser_registration import run_cloak_registration
from core.email_provider import resolve_email_source
from core.gptaccount_driver import build_gptaccount_driver
from core.gptaccount_network import start_gptaccount_network

logger = logging.getLogger(__name__)


def _save_account_after_session_error(*, email, name, password, error, proxy):
    """保留 GptAccount 已创建但未取得 Token 的账号；不触发依赖 Token 的后续任务。"""
    existing = db.get_account_by_email(email)
    if existing is not None:
        return int(existing["id"])
    account_id = db.insert_account(
        email=email,
        access_token="",
        user_name=name,
        email_source=resolve_email_source(email),
        proxy_used=proxy,
        extra={
            "registration_password": password,
            "registration_session_error": str(error)[:500],
        },
    )
    db.update_account_note(
        account_id,
        f"已创建，注册时获取 Token 失败；可通过查活补取。原始错误：{str(error)[:300]}",
    )
    logger.warning("[GptAccount注册] 账号已保留到列表，获取 Token 失败，任务仍标失败：id=%s email=%s", account_id, email)
    return account_id


def capture_browser_state(driver):
    try:
        state = driver.context.storage_state()
    finally:
        # Stop background downloads even if the snapshot fails. Keep the CDP
        # target alive until accounting finishes and preserve the snapshot error.
        try:
            driver.get("about:blank")
        except Exception:
            pass
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "format": "playwright-storage-state",
        "storage_state": state,
    }


def run_gptaccount_registration(**kwargs):
    return run_cloak_registration(
        **kwargs, browser_factory=build_gptaccount_driver, provider="gptaccount",
        capture_state=capture_browser_state, network_factory=start_gptaccount_network,
        on_session_error=_save_account_after_session_error,
    )
