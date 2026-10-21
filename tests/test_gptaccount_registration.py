import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import cloakbrowser_registration as flow
from core import gptaccount_driver as driver_module
from core.gptaccount_registration import capture_browser_state, run_gptaccount_registration


@contextmanager
def isolated_account_storage():
    from core import db

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        # Redirect both the active DB selector and all legacy migration sources.
        paths = {key: root / key for key, value in vars(db).items()
                 if isinstance(value, Path) and not key.startswith("_DEFAULT_")}
        paths.update(_SQLITE_READY=False, _SQLITE_READY_PATH=None)
        with patch.multiple(db, **paths):
            assert db._active_sqlite_path().parent == root
            yield db


class GptAccountRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.driver = Mock()
        self.driver.context.storage_state.return_value = {
            "cookies": [{"name": "session", "value": "cookie-secret", "domain": ".chatgpt.com"}],
            "origins": [{"origin": "https://chatgpt.com", "localStorage": [{"name": "key", "value": "value"}]}],
        }
        self.driver.proxy_transport_snapshot.return_value = None
        self.opened = SimpleNamespace(profile_id="ephemeral", raw={"proxy_pool_target": "http://proxy.example:8080"})
        self.factory = self.stack.enter_context(patch("core.gptaccount_registration.build_gptaccount_driver", return_value=(self.driver, self.opened)))
        self.save = self.stack.enter_context(patch.object(flow, "save_account_data", return_value=42))
        self.stack.enter_context(patch.object(flow, "PlaywrightTrafficTracker", side_effect=RuntimeError("test-no-tracker")))
        self.saver = self.stack.enter_context(patch.object(flow, "BrowserDataSaver"))
        for name in ["human_delay", "post_register_dwell", "_check_manual_stop", "_maybe_accept", "_clear_otp_inputs", "_type_otp", "_click_continue"]:
            self.stack.enter_context(patch.object(flow, name))
        self.stack.enter_context(patch.object(flow, "_submit_email_and_wait_next", return_value="otp"))
        self.stack.enter_context(patch.object(flow, "_fill_password_page_if_present", return_value="fixture-password"))
        self.stack.enter_context(patch.object(flow, "_wait_after_email_otp_submit", return_value="accepted"))
        self.stack.enter_context(patch.object(flow, "_complete_profile_page", return_value=True))
        self.stack.enter_context(patch.object(flow, "_fetch_chatgpt_session", return_value={"accessToken": "fixture-token", "user": {"id": "u"}}))
        self.stack.enter_context(patch.object(flow, "resolve_email_source", return_value="cloudflare_domain"))
        self.release = self.stack.enter_context(patch("core.email_provider.release_email"))

    def run_flow(self):
        return run_gptaccount_registration(email="fixture@example.com", name="Fixture", birthday="1995-01-01", otp_code="123456")

    def test_saves_credentials_and_storage_state_then_closes(self):
        with patch("config.codex.ENABLE_CODEX_AUTO", True), patch("core.roxy_codex_oauth.run_roxy_codex_oauth") as codex:
            result = self.run_flow()
        self.assertTrue(result["success"], result)
        payload = self.save.call_args.kwargs
        self.assertEqual(payload["access_token"], "fixture-token")
        self.assertEqual(payload["extra"]["registration_password"], "fixture-password")
        snapshot = payload["extra"]["browser_session"]
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["storage_state"]["cookies"][0]["value"], "cookie-secret")
        self.assertTrue(snapshot["storage_state"]["origins"])
        self.assertIn("gptaccount", payload["extra"])
        self.assertEqual(payload["proxy_used"], "http://proxy.example:8080")
        self.driver.quit.assert_called_once()
        codex.assert_not_called()

    def test_snapshot_failure_keeps_registered_account_and_reports_partial_failure(self):
        self.driver.context.storage_state.side_effect = RuntimeError("browser gone")
        result = self.run_flow()
        self.assertFalse(result["success"])
        self.assertEqual(result["account_id"], 42)
        self.assertIn("Cookies", result["error"])
        self.save.assert_called_once()
        self.driver.quit.assert_called_once()

    def test_cancellation_at_checkpoint_closes_browser(self):
        from core.registration_service import StopRequested
        with patch.object(flow, "_check_manual_stop", side_effect=StopRequested("stopped")):
            result = self.run_flow()
        self.assertFalse(result["success"])
        self.save.assert_not_called()
        self.driver.quit.assert_called_once()

    def test_cleanup_failure_is_not_reported_as_success(self):
        self.driver.quit.side_effect = RuntimeError("cleanup not confirmed")
        result = self.run_flow()
        self.assertFalse(result["success"])
        self.assertEqual(result["account_id"], 42)
        self.assertIn("cleanup not confirmed", result["error"])

    def test_telemetry_failure_still_closes_browser(self):
        self.saver.return_value.stop.side_effect = RuntimeError("telemetry failed")
        with self.assertRaisesRegex(RuntimeError, "telemetry failed"):
            self.run_flow()
        self.driver.quit.assert_called_once()

    def test_dwell_failure_does_not_lose_registered_account(self):
        with patch.object(flow, "post_register_dwell", side_effect=RuntimeError("stopped after save")):
            result = self.run_flow()
        self.assertEqual(result["account_id"], 42)
        self.save.assert_called_once()
        self.driver.quit.assert_called_once()
        self.release.assert_not_called()

    def test_stop_while_waiting_for_mail_does_not_submit_or_resend(self):
        from core.registration_service import StopRequested

        for mail_fails in (False, True):
            with self.subTest(mail_fails=mail_fails):
                stopped = False

                def wait(*args, **kwargs):
                    nonlocal stopped
                    stopped = True
                    if mail_fails:
                        raise TimeoutError("mail timeout")
                    return "123456"

                def check():
                    if stopped:
                        raise StopRequested("stopped while waiting for mail")

                with patch.object(flow, "wait_for_otp", side_effect=wait), patch.object(flow, "_check_manual_stop", side_effect=check), patch.object(flow, "_type_otp") as submit, patch.object(flow, "_click_resend_email_otp") as resend:
                    result = run_gptaccount_registration(email="fixture@example.com", name="Fixture", birthday="1995-01-01")
                self.assertFalse(result["success"])
                self.assertIn("stopped while waiting for mail", result["error"])
                submit.assert_not_called()
                resend.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(self.driver.quit.call_count, 2)

    def test_cloak_still_uses_its_factory_and_keep_open_setting(self):
        with patch.object(flow, "build_cloak_driver", return_value=(self.driver, self.opened)) as factory, patch.object(flow._cfg, "CLOAK_KEEP_BROWSER_OPEN", True), patch("config.codex.ENABLE_CODEX_AUTO", False):
            result = flow.run_cloak_registration("fixture@example.com", "Fixture", "1995-01-01", otp_code="123456")
        self.assertTrue(result["success"], result)
        factory.assert_called_once()
        self.factory.assert_not_called()
        self.driver.context.storage_state.assert_not_called()
        self.driver.quit.assert_not_called()
        self.assertIn("cloakbrowser", self.save.call_args.kwargs["extra"])


class GptAccountDriverTests(unittest.TestCase):
    def test_startup_error_is_preserved_when_cleanup_also_fails(self):
        bridge = Mock()
        bridge.open.side_effect = RuntimeError("Chrome not found")
        bridge.close.side_effect = RuntimeError("cleanup not confirmed")
        with patch.object(driver_module, "GptAccountBridge", return_value=bridge), patch("core.proxy_chain.open_proxy_pool_proxy", return_value=("", None)):
            with self.assertLogs(driver_module.logger, level="WARNING") as logs:
                with self.assertRaisesRegex(RuntimeError, "Chrome not found"):
                    driver_module.build_gptaccount_driver("")
        self.assertIn("cleanup not confirmed", " ".join(logs.output))

    def test_attach_failure_closes_bridge_playwright_and_proxy_relay(self):
        bridge, relay, playwright = Mock(), Mock(), Mock()
        playwright.chromium.connect_over_cdp.side_effect = RuntimeError("CDP rejected")
        with patch.object(driver_module, "GptAccountBridge", return_value=bridge), patch("playwright.sync_api.sync_playwright") as start, patch("core.proxy_chain.open_proxy_pool_proxy", return_value=("http://127.0.0.1:1234", relay)) as chain:
            start.return_value.start.return_value = playwright
            with self.assertRaisesRegex(RuntimeError, "CDP rejected"):
                driver_module.build_gptaccount_driver("host.example:8080:user:pass")
        chain.assert_called_once_with("http://user:pass@host.example:8080", measure_transport=True)
        bridge.close.assert_called_once()
        playwright.stop.assert_called_once()
        relay.close.assert_called_once()

    def test_quit_is_idempotent_and_releases_relay_even_on_cleanup_error(self):
        bridge, relay, playwright = Mock(), Mock(), Mock()
        bridge.close.side_effect = RuntimeError("cleanup failed")
        driver = driver_module.GptAccountDriver(bridge=bridge, playwright=playwright, browser=Mock(), context=Mock(), page=Mock(), proxy_relay=relay)
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            driver.quit()
        driver.quit()
        relay.close.assert_called_once()
        playwright.stop.assert_called_once()

    def test_storage_snapshot_survives_sqlite_and_automatic_twofa_is_queued(self):
        from core import db
        from core.account_export import save_account_data
        snapshot = capture_browser_state(SimpleNamespace(context=SimpleNamespace(storage_state=lambda: {"cookies": [{"name": "session", "value": "fixture"}], "origins": []})))
        with isolated_account_storage(), patch("config.twofa.ENABLE_2FA", True), patch("core.twofa_service.enqueue_account_totp_setup", return_value={"accepted": True}) as twofa:
            account_id = save_account_data("fixture@example.com", "fixture-token", extra={"browser_session": snapshot, "registration_password": "fixture-pass"}, proxy_used="http://proxy.example:80", auto_plan_check=False)
            row = db.get_account(account_id)
            self.assertEqual(json.loads(row["extra_json"])["browser_session"], snapshot)
            self.assertEqual(row["access_token"], "fixture-token")
            self.assertEqual(twofa.call_args.kwargs["proxy"], "http://proxy.example:80")

    def test_capture_closes_old_pages_after_snapshot(self):
        old, fresh = Mock(), Mock()
        context = Mock(pages=[old])
        context.storage_state.return_value = {"cookies": [], "origins": []}
        context.new_page.return_value = fresh
        driver = SimpleNamespace(context=context, page=old)
        snapshot = capture_browser_state(driver)
        self.assertEqual(snapshot["storage_state"], {"cookies": [], "origins": []})
        old.close.assert_called_once()
        self.assertIs(driver.page, fresh)


if __name__ == "__main__":
    unittest.main()
