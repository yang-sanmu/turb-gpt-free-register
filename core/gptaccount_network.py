"""Reuse Roxy's CDP blocking, static cache and byte accounting with GptAccount."""
from __future__ import annotations

import logging

from core.browser_data_saver import BrowserDataSaver
from core.browser_traffic import SeleniumTrafficTracker
from core.roxy_asset_cache import RoxyLocalAssetCache

logger = logging.getLogger(__name__)

_NETWORK_EVENTS = (
    "requestWillBeSent", "responseReceived", "dataReceived", "loadingFinished",
    "loadingFailed", "requestServedFromCache", "webSocketCreated",
    "webSocketWillSendHandshakeRequest", "webSocketHandshakeResponseReceived",
    "webSocketFrameSent", "webSocketFrameReceived", "webSocketClosed",
)


class _CDPLogAdapter:
    """Present Playwright CDP events in the existing Chrome performance-log format."""

    def __init__(self, driver):
        self.session = driver.context.new_cdp_session(driver.page)
        self.execute_script = driver.execute_script
        self.entries = []
        for event in _NETWORK_EVENTS:
            method = f"Network.{event}"
            self.session.on(method, lambda params, method=method: self.entries.append(
                {"message": {"message": {"method": method, "params": params}}}
            ))

    def execute_cdp_cmd(self, method, params=None):
        return self.session.send(method, params or {})

    def get_log(self, _kind):
        # Flush events queued during blocking Python work before taking the log.
        try:
            self.session.send("Runtime.evaluate", {"expression": "0"})
        except Exception:
            pass  # Retain already received events when the page has crashed.
        entries, self.entries = self.entries, []
        return entries

    def close(self):
        try:
            self.session.detach()
        except Exception:
            pass  # The owned browser/page may already have closed.
        self.entries.clear()


class GptAccountTrafficTracker(SeleniumTrafficTracker):
    def __init__(self, driver, *, label="GptAccount", use_cache=False):
        adapter = _CDPLogAdapter(driver)
        super().__init__(adapter, label=label)
        self.method = "gptaccount.cdp.Network"
        self.asset_cache = None
        self._cache_snapshot = None
        if use_cache:
            try:
                self.asset_cache = RoxyLocalAssetCache(driver.debugger_address, label=label)
                self.asset_cache.start()
                self.attach_local_asset_cache(self.asset_cache)
            except Exception as exc:
                logger.warning("[%s] 本地缓存启动失败，继续联网加载：%s", label, exc)

    def stop(self):
        if not self._stopped:
            try:
                if self.asset_cache is not None:
                    self._cache_snapshot = self.asset_cache.stop()
                result = super().stop()
            finally:
                self.driver.close()
        else:
            result = super().stop()
        if self._cache_snapshot is not None:
            # Roxy's bytes_saved measures decoded cached bodies, not billed bytes.
            result["local_asset_cache"] = {
                **self._cache_snapshot, "bytes_saved_basis": "decoded_response_body",
            }
        return result


def start_gptaccount_network(driver, *, label="GptAccount"):
    saver = BrowserDataSaver(label=label)
    try:
        tracker = GptAccountTrafficTracker(driver, label=label, use_cache=saver.enabled)
    except Exception as exc:
        # Like the upstream flow, optional accounting must not prevent signup.
        logger.warning("[%s] 初始化流量统计失败，继续注册（本轮无共享缓存）：%s", label, exc)
        saver.install_selenium(driver)
        return None, saver
    tracker.attach_data_saver(saver)
    # CDP URL blocking leaves Playwright routing disabled and works with the
    # same Fetch-based disk cache used by Roxy, across ephemeral profiles.
    saver.install_selenium(tracker.driver)
    return tracker, saver
