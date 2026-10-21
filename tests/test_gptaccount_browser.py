"""Opt-in real Chrome checks; all page requests use local test fixtures.

PowerShell: $env:GPTACCOUNT_BROWSER_TEST='1'
python -m unittest discover -s tests -p test_gptaccount_browser.py -v
"""
import base64
import gzip
import os
import random
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from config import gptaccount as cfg
from core.gptaccount_driver import GptAccountBridge, build_gptaccount_driver
from core.gptaccount_registration import capture_browser_state
from core.gptaccount_network import start_gptaccount_network


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
                    # The upstream birthday fallback replaces selected text without
                    # Backspace; send_keys must preserve that selection.
                    driver.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
                        '<input role="spinbutton" data-type="year" value="2000">' +
                        '<input role="spinbutton" data-type="month" value="01">' +
                        '<input role="spinbutton" data-type="day" value="01">')""")
                    from core.roxy_registration import _fill_birthday_or_age
                    self.assertEqual(_fill_birthday_or_age(driver, "1995-06-17", 31), "spinbutton")
                    self.assertEqual(driver.page.locator('[role="spinbutton"]').evaluate_all(
                        "elements => elements.map(el => el.value)"), ["1995", "06", "17"])
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


class CacheProxyFixture(ProxyFixture):
    # A compressed script proves that cached body bytes are not wire bytes.
    script = b"window.cacheFixture = true;/*" + bytes(random.Random(7).choices(
        b"abcdefghijklmnopqrstuvwxyz0123456789", k=192000)) + b"*/"
    encoded_script = gzip.compress(script)

    def do_GET(self):
        self.server.requests.append(self.path)
        if self.path.startswith("http://registration-fixture.invalid/"):
            version = "v2" if "v2" in self.path else "v1"
            body = (f'<script src="http://fixture.oaistatic.com/assets/app.{version}.js"></script>'
                    '<link rel="stylesheet" href="http://fixture.oaistatic.com/assets/style.css">'
                    '<script>fetch("http://fixture.oaistatic.com/api/session")'
                    '.then(r => r.json()).then(() => window.apiFixture = true)</script>').encode()
            content_type = "text/html"
        elif self.path.startswith("http://fixture.oaistatic.com/assets/app."):
            body, content_type = self.encoded_script, "application/javascript"
        elif self.path == "http://fixture.oaistatic.com/api/session":
            body, content_type = b'{"ok":true}', "application/json"
        elif self.path.endswith("/style.css"):
            body, content_type = b"body{color:black}", "text/css"
        else:
            self.send_error(403)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=3600" if "/assets/" in self.path else "no-store")
        if content_type == "application/javascript":
            self.send_header("Content-Encoding", "gzip")
        if content_type == "text/html":
            self.send_header("Set-Cookie", "fixture_session=private; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(os.environ.get("GPTACCOUNT_BROWSER_TEST") == "1", "opt-in real Chrome test")
class GptAccountCacheBrowserTests(unittest.TestCase):
    def test_fresh_profiles_share_static_cache_but_not_sessions_or_api(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), CacheProxyFixture)
        server.requests = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        proxy = f"http://127.0.0.1:{server.server_port}"
        results = {}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cfg, "GPTACCOUNT_HEADLESS", True), \
                patch("config.proxy.PROXY_POOL_UPSTREAM_PROXY", ""), \
                patch("config.browser.BROWSER_JS_COVERAGE_LOG", False), \
                patch("config.browser.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["image", "stylesheet"]), \
                patch("config.roxybrowser.ROXY_LOCAL_ASSET_CACHE_DIR", directory), \
                patch("config.roxybrowser.ROXY_LOCAL_ASSET_CACHE_MODE", "auto"):
            for name, enabled, version in [("cold", True, "v1"), ("warm", True, "v1"),
                                           ("changed", True, "v2"), ("disabled", False, "v1")]:
                with self.subTest(name=name), patch("config.browser.BROWSER_DATA_SAVER_MODE", enabled):
                    driver = tracker = saver = None
                    before = len(server.requests)
                    try:
                        driver, _ = build_gptaccount_driver(proxy)
                        runtime = Path(driver._bridge.runtime_dir)
                        self.assertFalse(driver.context.cookies("http://registration-fixture.invalid/"))
                        tracker, saver = start_gptaccount_network(driver)
                        with patch.object(driver.context, "route", side_effect=AssertionError("routing must stay off")):
                            driver.get(f"http://registration-fixture.invalid/{version}")
                            driver.page.wait_for_function("window.cacheFixture && window.apiFixture", timeout=10000)
                        if enabled:
                            deadline = time.monotonic() + 5
                            while tracker.asset_cache.recorded + tracker.asset_cache.cache_hits < 1 and time.monotonic() < deadline:
                                driver.page.wait_for_timeout(20)
                        snapshot = capture_browser_state(driver)
                        self.assertTrue(snapshot["storage_state"]["cookies"])
                        traffic = tracker.stop()
                        self.assertEqual(tracker.stop(), traffic)
                        transport = driver.proxy_transport_snapshot()
                        paths = server.requests[before:]
                        self.assertIn("http://fixture.oaistatic.com/api/session", paths)
                        self.assertEqual(any(path.endswith("style.css") for path in paths), not enabled)
                        self.assertEqual(any(path.endswith(f"app.{version}.js") for path in paths), name != "warm")
                        results[name] = transport["total_bytes"]
                        if name == "warm":
                            cache = traffic["local_asset_cache"]
                            self.assertGreaterEqual(cache["hits"], 1)
                            self.assertEqual(cache["bytes_saved"], len(CacheProxyFixture.script))
                            self.assertLess(traffic["download_bytes"], len(CacheProxyFixture.encoded_script) / 4)
                            self.assertLess(transport["total_bytes"], len(CacheProxyFixture.encoded_script) / 4)
                        elif enabled:
                            self.assertGreaterEqual(traffic["local_asset_cache"]["recorded"], 1)
                            self.assertGreaterEqual(transport["download_bytes"], len(CacheProxyFixture.encoded_script))
                        else:
                            self.assertNotIn("local_asset_cache", traffic)
                    finally:
                        if tracker is not None:
                            tracker.stop()
                        if saver is not None:
                            saver.stop()
                        if driver is not None:
                            driver.quit()
                    self.assertFalse(runtime.exists())
        print("Cache fixture proxy bytes:", results)


if __name__ == "__main__":
    unittest.main()
