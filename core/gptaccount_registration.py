"""Use the maintained upstream registration steps with GptAccount Chrome."""
from datetime import datetime, timezone

from core.cloakbrowser_registration import run_cloak_registration
from core.gptaccount_driver import build_gptaccount_driver


def capture_browser_state(driver):
    state = driver.context.storage_state()
    _close_pages_after_capture(driver)
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "format": "playwright-storage-state",
        "storage_state": state,
    }


def _close_pages_after_capture(driver):
    """ChatGPT keeps long-lived requests open; the upstream traffic tracker then
    blocks forever in Request.sizes() on stop. Nothing uses the page after the
    snapshot on this path, so close it and let those requests fail."""
    try:
        context = driver.context
        old_pages = list(context.pages)
        driver.page = context.new_page()
        for page in old_pages:
            page.close()
    except Exception:
        pass


def run_gptaccount_registration(**kwargs):
    return run_cloak_registration(
        **kwargs, browser_factory=build_gptaccount_driver, provider="gptaccount",
        capture_state=capture_browser_state,
    )
