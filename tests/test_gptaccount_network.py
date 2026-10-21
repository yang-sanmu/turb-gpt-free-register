import unittest
from unittest.mock import Mock, patch

from core.gptaccount_network import start_gptaccount_network


class GptAccountNetworkTests(unittest.TestCase):
    def test_cdp_accounting_excludes_local_cache_and_blocked_requests(self):
        driver = Mock(debugger_address="http://127.0.0.1:9222")
        handlers = {}
        session = driver.context.new_cdp_session.return_value
        session.on.side_effect = lambda method, handler: handlers.update({method: handler})
        with patch("config.browser.BROWSER_DATA_SAVER_MODE", True), \
                patch("config.browser.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["image"]), \
                patch("config.browser.BROWSER_JS_COVERAGE_LOG", False), \
                patch("core.gptaccount_network.RoxyLocalAssetCache") as cache_factory:
            cache = cache_factory.return_value
            cache.was_fulfilled_network_request.side_effect = lambda request_id: request_id == "cached"
            cache.stop.return_value = {"hits": 1, "bytes_saved": 500000}
            tracker, saver = start_gptaccount_network(driver)

            def request(key, path, resource_type):
                handlers["Network.requestWillBeSent"]({
                    "requestId": key, "type": resource_type,
                    "request": {"url": "https://chatgpt.com/" + path, "method": "GET", "headers": {}},
                })

            request("cached", "cdn/assets/cached.js", "Script")
            handlers["Network.responseReceived"]({"requestId": "cached", "response": {"status": 200}})
            handlers["Network.loadingFinished"]({"requestId": "cached", "encodedDataLength": 500000})
            request("blocked", "image.png", "Image")
            handlers["Network.loadingFailed"]({"requestId": "blocked", "blockedReason": "inspector"})
            request("api", "api/auth/session", "Fetch")
            handlers["Network.loadingFinished"]({"requestId": "api", "encodedDataLength": 123})
            result = tracker.stop()
            self.assertEqual(result["download_bytes"], 123)
            self.assertEqual(result["data_saver_blocked_count"], 1)
            self.assertEqual(result["local_asset_cache"]["hits"], 1)
            self.assertEqual(result["local_asset_cache"]["bytes_saved_basis"], "decoded_response_body")
            self.assertEqual(tracker.stop(), result)
            cache.stop.assert_called_once()
            session.detach.assert_called_once()
            driver.context.route.assert_not_called()
            saver.stop()

    def test_disabled_mode_collects_traffic_without_cache_or_routing(self):
        driver = Mock()
        with patch("config.browser.BROWSER_DATA_SAVER_MODE", False), \
                patch("config.browser.BROWSER_JS_COVERAGE_LOG", False), \
                patch("core.gptaccount_network.RoxyLocalAssetCache") as cache:
            tracker, saver = start_gptaccount_network(driver)
            result = tracker.stop()
            self.assertFalse(result["data_saver_enabled"])
            self.assertNotIn("local_asset_cache", result)
            cache.assert_not_called()
            driver.context.route.assert_not_called()
            saver.stop()


if __name__ == "__main__":
    unittest.main()
