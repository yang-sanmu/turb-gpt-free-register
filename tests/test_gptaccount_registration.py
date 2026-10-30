import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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
        self.network = self.stack.enter_context(patch(
            "core.gptaccount_registration.start_gptaccount_network",
            return_value=(None, self.saver.return_value),
        ))
        for name in ["human_delay", "post_register_dwell", "_check_manual_stop", "_maybe_accept", "_clear_otp_inputs", "_type_otp", "_click_continue"]:
            self.stack.enter_context(patch.object(flow, name))
        self.stack.enter_context(patch.object(flow, "_submit_email_and_wait_next", return_value="otp"))
        self.stack.enter_context(patch.object(flow, "_fill_password_page_if_present", return_value="fixture-password"))
        self.stack.enter_context(patch.object(flow, "_wait_after_email_otp_submit", return_value="accepted"))
        self.stack.enter_context(patch.object(flow, "_complete_profile_page", return_value=True))
        self.stack.enter_context(patch.object(flow, "_fetch_chatgpt_session", return_value={"accessToken": "fixture-token", "user": {"id": "u"}}))
        self.stack.enter_context(patch.object(flow, "resolve_email_source", return_value="cloudflare_domain"))
        self.stack.enter_context(patch("core.gptaccount_registration.resolve_email_source", return_value="cloudflare_domain"))
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
        self.network.assert_called_once_with(self.driver, label="GptAccount")
        codex.assert_not_called()

    def test_snapshot_failure_keeps_registered_account_and_reports_partial_failure(self):
        self.driver.context.storage_state.side_effect = RuntimeError("browser gone")
        result = self.run_flow()
        self.assertFalse(result["success"])
        self.assertEqual(result["account_id"], 42)
        self.assertIn("Cookies", result["error"])
        self.save.assert_called_once()
        self.driver.get.assert_any_call("about:blank")
        self.driver.quit.assert_called_once()

    def test_session_timeout_saves_account_and_password_without_post_registration_tasks(self):
        with isolated_account_storage() as db, patch.object(
            flow, "_fetch_chatgpt_session", side_effect=RuntimeError("session 暂无 accessToken"),
        ), patch("config.twofa.ENABLE_2FA", True), patch(
            "config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", True,
        ), patch("core.twofa_service.enqueue_account_totp_setup") as twofa, patch(
            "core.plan_check_service.enqueue_account_plan_check",
        ) as plan:
            result = self.run_flow()
            row = db.get_account(result["account_id"])
            self.assertFalse(result["success"])
            self.assertIn("session 暂无 accessToken", result["error"])
            self.assertEqual(row["email"], "fixture@example.com")
            self.assertEqual(row["access_token"], "")
            self.assertEqual(row["user_name"], "Fixture")
            extra = json.loads(row["extra_json"])
            self.assertEqual(extra["registration_password"], "fixture-password")
            self.assertEqual(extra["registration_session_error"], "session 暂无 accessToken")
            self.assertIn("已创建，注册时获取 Token 失败", row["note"])
            self.assertIn("session 暂无 accessToken", row["note"])
            twofa.assert_not_called()
            plan.assert_not_called()
        self.save.assert_not_called()
        self.release.assert_not_called()
        self.driver.quit.assert_called_once()

    def test_failure_before_profile_submission_does_not_save_account(self):
        with isolated_account_storage() as db, patch.object(
            flow, "_complete_profile_page", side_effect=RuntimeError("profile not submitted"),
        ):
            result = self.run_flow()
            self.assertIsNone(db.get_account_by_email("fixture@example.com"))
        self.assertFalse(result["success"])
        self.assertIsNone(result["account_id"])
        self.save.assert_not_called()
        self.assertEqual(self.release.call_args.kwargs["status"], "available")

    def test_session_timeout_preserves_existing_account_credentials_and_note(self):
        with isolated_account_storage() as db, patch.object(
            flow, "_fetch_chatgpt_session", side_effect=RuntimeError("session timeout"),
        ):
            account_id = db.insert_account(
                email="fixture@example.com", access_token="existing-token",
                extra={"registration_password": "existing-password"},
            )
            db.update_account_note(account_id, "用户备注")
            original = db.get_account(account_id)
            result = self.run_flow()
            self.assertEqual(db.get_account(account_id), original)
        self.assertFalse(result["success"])
        self.assertEqual(result["account_id"], account_id)
        self.release.assert_not_called()

    def test_partial_save_failure_preserves_original_session_error(self):
        with patch.object(
            flow, "_fetch_chatgpt_session", side_effect=RuntimeError("original session timeout"),
        ), patch("core.db.get_account_by_email", return_value=None), patch(
            "core.db.insert_account", side_effect=RuntimeError("database unavailable"),
        ), self.assertLogs(flow.logger, level="WARNING") as logs:
            result = self.run_flow()
        self.assertFalse(result["success"])
        self.assertIsNone(result["account_id"])
        self.assertIn("original session timeout", result["error"])
        self.assertIn("database unavailable", " ".join(logs.output))
        self.assertEqual(self.release.call_args.kwargs["status"], "failed")

    def test_successful_registration_still_queues_plan_and_twofa(self):
        from core.account_export import save_account_data

        self.save.side_effect = save_account_data
        with isolated_account_storage() as db, patch("config.twofa.ENABLE_2FA", True), patch(
            "config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", True,
        ), patch("core.twofa_service.enqueue_account_totp_setup", return_value={"accepted": True}) as twofa, patch(
            "core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True},
        ) as plan:
            result = self.run_flow()
            self.assertTrue(result["success"], result)
            self.assertEqual(db.get_account(result["account_id"])["access_token"], "fixture-token")
            for enqueue in (twofa, plan):
                enqueue.assert_called_once()
                self.assertEqual(enqueue.call_args.kwargs["account_id"], result["account_id"])
                self.assertEqual(enqueue.call_args.kwargs["access_token"], "fixture-token")

    def test_cdp_statistics_start_failure_does_not_abort_registration(self):
        from core.gptaccount_network import start_gptaccount_network

        self.network.side_effect = start_gptaccount_network
        self.driver.context.new_cdp_session.side_effect = RuntimeError("CDP unavailable")
        with patch("config.browser.BROWSER_DATA_SAVER_MODE", True):
            result = self.run_flow()
        self.assertTrue(result["success"], result)
        self.assertIsNone(result["network_traffic"])
        self.save.assert_called_once()
        self.driver.execute_cdp_cmd.assert_called()
        self.driver.context.route.assert_not_called()
        self.driver.quit.assert_called_once()

    def test_cache_traffic_is_saved_on_success_and_returned_on_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                tracker = Mock()
                traffic = {"total_bytes": 123, "local_asset_cache": {"hits": 4}}
                tracker.stop.return_value = traffic
                self.network.return_value = (tracker, self.saver.return_value)
                with patch.object(flow, "_submit_email_and_wait_next", side_effect=RuntimeError("fixture failure") if fail else None):
                    result = self.run_flow()
                self.assertEqual(result["network_traffic"], traffic)
                self.assertEqual(result["success"], not fail)
                if not fail:
                    self.assertEqual(self.save.call_args.kwargs["extra"]["network_traffic"], traffic)
                self.assertTrue(tracker.stop.called)

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
        self.network.assert_not_called()
        self.saver.return_value.install_playwright.assert_called_once_with(self.driver.context)
        self.driver.context.storage_state.assert_not_called()
        self.driver.quit.assert_not_called()
        self.assertIn("cloakbrowser", self.save.call_args.kwargs["extra"])

    def test_cloak_session_timeout_keeps_upstream_behavior_without_saving(self):
        with patch.object(flow, "build_cloak_driver", return_value=(self.driver, self.opened)), patch.object(
            flow, "_fetch_chatgpt_session", side_effect=RuntimeError("session timeout"),
        ), patch("core.db.insert_account") as insert:
            result = flow.run_cloak_registration(
                "fixture@example.com", "Fixture", "1995-01-01", otp_code="123456",
            )
        self.assertFalse(result["success"])
        self.assertIsNone(result["account_id"])
        self.save.assert_not_called()
        insert.assert_not_called()
        self.assertEqual(self.release.call_args.kwargs["status"], "failed")


class GptAccountDriverTests(unittest.TestCase):
    def test_startup_is_serial_but_ready_browsers_can_continue_concurrently(self):
        start_together = threading.Barrier(3)
        after_startup = threading.Barrier(3)
        state_lock = threading.Lock()
        active = peak = 0

        def initialize(proxy):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)  # Give other workers time to contend during startup.
            with state_lock:
                active -= 1
            return proxy

        def worker(index):
            start_together.wait(timeout=5)
            result = driver_module.build_gptaccount_driver(str(index))
            # All browsers must return from initialization before any is closed.
            after_startup.wait(timeout=5)
            return result

        with patch.object(driver_module, "_build_gptaccount_driver", side_effect=initialize):
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(worker, range(3)))
        self.assertEqual(results, ["0", "1", "2"])
        self.assertEqual(peak, 1)

    def test_waiting_worker_can_cancel_without_releasing_another_workers_lock(self):
        from core.registration_service import StopRequested

        owner_started = threading.Event()
        release_owner = threading.Event()
        waiter_checked = threading.Event()
        cancel_waiter = threading.Event()
        owner_done = threading.Event()

        def initialize(proxy):
            self.assertEqual(proxy, "owner")
            owner_started.set()
            self.assertTrue(release_owner.wait(timeout=5))
            return proxy

        def owner():
            try:
                return driver_module.build_gptaccount_driver("owner")
            finally:
                owner_done.set()

        def waiter():
            def check_stop():
                waiter_checked.set()
                if cancel_waiter.is_set():
                    raise StopRequested("cancelled while waiting")
            with patch("core.registration_service.check_stop_requested", side_effect=check_stop):
                return driver_module.build_gptaccount_driver("waiter")

        with patch.object(driver_module, "_build_gptaccount_driver", side_effect=initialize) as build:
            with ThreadPoolExecutor(max_workers=2) as pool:
                owning = pool.submit(owner)
                try:
                    self.assertTrue(owner_started.wait(timeout=5))
                    waiting = pool.submit(waiter)
                    self.assertTrue(waiter_checked.wait(timeout=5))
                    cancel_waiter.set()
                    with self.assertRaisesRegex(StopRequested, "cancelled while waiting"):
                        waiting.result(timeout=3)
                    self.assertFalse(owner_done.is_set())
                    self.assertTrue(driver_module._GPTACCOUNT_START_LOCK.locked())
                    build.assert_called_once_with("owner")
                finally:
                    release_owner.set()
                self.assertEqual(owning.result(timeout=5), "owner")
        self.assertFalse(driver_module._GPTACCOUNT_START_LOCK.locked())

    def test_startup_error_is_preserved_when_cleanup_also_fails(self):
        bridge = Mock()
        bridge.open.side_effect = RuntimeError("Chrome not found")
        bridge.close.side_effect = RuntimeError("cleanup not confirmed")
        with patch.object(driver_module, "GptAccountBridge", return_value=bridge), patch("core.proxy_chain.open_proxy_pool_proxy", return_value=("", None)):
            with self.assertLogs(driver_module.logger, level="WARNING") as logs:
                with self.assertRaisesRegex(RuntimeError, "Chrome not found"):
                    driver_module.build_gptaccount_driver("")
        self.assertIn("cleanup not confirmed", " ".join(logs.output))
        self.assertFalse(driver_module._GPTACCOUNT_START_LOCK.locked())

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
        self.assertFalse(driver_module._GPTACCOUNT_START_LOCK.locked())

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

    def test_capture_preserves_state_before_stopping_page_downloads(self):
        driver = Mock()
        state = {"cookies": [{"name": "session", "value": "fixture"}], "origins": []}
        driver.context.storage_state.return_value = state
        driver.get.side_effect = lambda url: driver.context.storage_state.assert_called_once()
        snapshot = capture_browser_state(driver)
        self.assertEqual(snapshot["storage_state"], state)
        driver.get.assert_called_once_with("about:blank")


if __name__ == "__main__":
    unittest.main()
