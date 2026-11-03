"""Use the maintained upstream registration steps with GptAccount Chrome."""
from datetime import datetime, timezone

from core.cloakbrowser_registration import run_cloak_registration
from core.gptaccount_driver import build_gptaccount_driver
from core.gptaccount_network import start_gptaccount_network
from core.registration_token_recovery import attach_create_account_probe, recover_session_after_timeout


def _start_network_with_probe(driver, *, label):
    attach_create_account_probe(driver)
    return start_gptaccount_network(driver, label=label)


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
        capture_state=capture_browser_state, network_factory=_start_network_with_probe,
        on_session_error=recover_session_after_timeout,
    )
