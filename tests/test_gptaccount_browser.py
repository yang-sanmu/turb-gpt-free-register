"""Opt-in real Chrome checks; all page requests use local test fixtures.

PowerShell: $env:GPTACCOUNT_BROWSER_TEST='1'
python -m unittest discover -s tests -p test_gptaccount_browser.py -v
"""
import base64
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from config import gptaccount as cfg
from core.gptaccount_driver import GptAccountBridge, build_gptaccount_driver
from core.gptaccount_registration import capture_browser_state


class ProxyFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Proxy-Authorization")))
        if not self.path.startswith("http://registration-fixture.invalid/"):
            self.send_error(403)
            return
        body = b'<title>GptAccount fixture</title><input name="email"><button onclick="document.title=document.querySelector(\'input\').value">Save</button>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "fixture_cookie=saved; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        # Never forward browser background HTTPS traffic during this fixture.
        self.send_error(403)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get("GPTACCOUNT_BROWSER_TEST") == "1", "opt-in real Chrome test")
class RealGptAccountBrowserTests(unittest.TestCase):
    def test_real_headed_and_headless_browser_proxy_storage_and_cleanup(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyFixture)
        server.requests = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        proxy = f"http://fixture-user:fixture-pass@127.0.0.1:{server.server_port}"
        expected_auth = "Basic " + base64.b64encode(b"fixture-user:fixture-pass").decode()
        for headless in (False, True):
            with self.subTest(headless=headless), patch.object(cfg, "GPTACCOUNT_HEADLESS", headless), patch.object(cfg, "GPTACCOUNT_TIMEZONE", "Asia/Tokyo"), patch("config.proxy.PROXY_POOL_UPSTREAM_PROXY", ""):
                driver = None
                try:
                    driver, opened = build_gptaccount_driver(proxy)
                    runtime = Path(driver._bridge.runtime_dir)
                    self.assertTrue(runtime.is_dir())
                    driver.get("http://registration-fixture.invalid/")
                    # The shared flow types char by char via Selenium-style send_keys.
                    from core.roxy_registration import _human_type_text
                    field = driver.find_element("css selector", "input")
                    _human_type_text(driver, field, "stale", clear=False)
                    _human_type_text(driver, field, "fixture@example.com", clear=True)
                    self.assertEqual(driver.page.locator("input").input_value(), "fixture@example.com")
                    # The shared flow clicks DOM nodes returned inside result objects.
                    found = driver.execute_script("return {ok: true, button: document.querySelector('button')};")
                    self.assertTrue(found["ok"])
                    found["button"].click()
                    self.assertEqual(driver.page.title(), "fixture@example.com")
                    self.assertEqual(driver.page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone"), "Asia/Tokyo")
                    driver.page.evaluate("localStorage.setItem('fixture_key', 'fixture_value')")
                    state = capture_browser_state(driver)["storage_state"]
                    self.assertTrue(any(c["name"] == "fixture_cookie" and c["httpOnly"] for c in state["cookies"]))
                    self.assertTrue(any(o["localStorage"] for o in state["origins"]))
                    self.assertTrue(any(auth == expected_auth for _, auth in server.requests))
                    self.assertEqual(opened.raw["proxy_pool_target"], proxy)
                finally:
                    if driver is not None:
                        driver.quit()
                self.assertFalse(runtime.exists(), "temporary profile remains after quit")
                self.assertTrue(driver._bridge.cleanup_result["cleaned"])

    def test_parent_pipe_disconnect_cleans_running_browser(self):
        with patch.object(cfg, "GPTACCOUNT_HEADLESS", True):
            bridge = GptAccountBridge()
            try:
                bridge.open("")
                runtime = Path(bridge.runtime_dir)
                bridge._stop.set()
                bridge.process.stdin.close()  # Same EOF the helper sees if Python exits.
            finally:
                result = bridge.close()
        self.assertEqual(result["reason"], "client-disconnected")
        self.assertTrue(result["cleaned"])
        self.assertFalse(runtime.exists())

    def test_missing_broker_fails_cleanly(self):
        with patch.dict(os.environ, {"GPT_ACCOUNT_KEEPER_CHROME_LAUNCHER": str(Path(__file__).parent / "missing-broker.exe")}):
            bridge = GptAccountBridge()
            try:
                with self.assertRaisesRegex(RuntimeError, "GptAccount"):
                    bridge.open("")
            finally:
                result = bridge.close()
        self.assertTrue(result["cleaned"])
        self.assertFalse(Path(result["runtime_dir"]).exists())

    def test_cancel_during_startup_cleans_directory(self):
        from core.registration_service import StopRequested
        before = set(Path(os.environ["TEMP"]).glob("gptaccount-registration-*"))
        with patch("core.registration_service.check_stop_requested", side_effect=StopRequested("fixture cancellation")):
            with self.assertRaises(StopRequested):
                build_gptaccount_driver("")
        self.assertEqual(set(Path(os.environ["TEMP"]).glob("gptaccount-registration-*")), before)


if __name__ == "__main__":
    unittest.main()
