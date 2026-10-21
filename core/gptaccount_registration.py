"""Use the maintained upstream registration steps with GptAccount Chrome."""
from datetime import datetime, timezone

from core.cloakbrowser_registration import run_cloak_registration
from core.gptaccount_driver import build_gptaccount_driver
from core.gptaccount_network import start_gptaccount_network


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
    )
