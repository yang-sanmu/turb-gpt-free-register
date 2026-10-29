# -*- coding: utf-8 -*-
import unittest
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch

import main as registration
from config import email as email_config
from config import icloud as icloud_config
from config import roxybrowser as driver_config
from core import browser_use_registration as browser_use
from core import email_provider
from core import icloud_mail_client as icloud
from core import roxy_registration as roxy
from core.registration_service import StopRequested


class EarlyICloudEmailAllocationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(email_config, USE_EMAIL_SERVICE=True, EMAIL_SOURCE="icloud"))
        self.stack.enter_context(patch.object(driver_config, "REGISTRATION_DRIVER", "gptaccount"))
        self.acquire = self.stack.enter_context(patch.object(
            registration, "acquire_email_from_source", return_value="one+fixed@icloud.com",
        ))
        self.release = self.stack.enter_context(patch.object(registration, "release_email_if_unconsumed"))

    def test_icloud_is_allocated_and_reported_before_each_browser_driver(self):
        drivers = {
            "gptaccount": "core.gptaccount_registration.run_gptaccount_registration",
            "roxy": "core.roxy_registration.run_roxy_registration",
            "cloak": "core.cloakbrowser_registration.run_cloak_registration",
            "browser_use": "core.browser_use_registration.run_browser_use_registration",
            "skyvern": "core.skyvern_registration.run_skyvern_registration",
        }
        for mode, target in drivers.items():
            with self.subTest(driver=mode):
                events = []
                self.acquire.reset_mock()
                self.acquire.side_effect = lambda source: events.append("acquire") or "one+fixed@icloud.com"
                callback = Mock(side_effect=lambda email: events.append("report"))
                with patch.object(driver_config, "REGISTRATION_DRIVER", mode), patch(
                    target, side_effect=lambda **kwargs: events.append("browser") or {"success": True},
                ) as driver:
                    registration.run_registration(None, "Test", on_email_acquired=callback)
                self.assertEqual(events, ["acquire", "report", "browser"])
                self.assertEqual(driver.call_args.kwargs["email"], "one+fixed@icloud.com")
                self.acquire.assert_called_once_with("icloud")
                callback.assert_called_once_with("one+fixed@icloud.com")
        self.release.assert_not_called()

    def test_exhausted_icloud_pool_does_not_start_browser(self):
        self.acquire.side_effect = icloud.ICloudMailError("邮箱均已使用")
        callback = Mock()
        with patch("core.gptaccount_registration.run_gptaccount_registration") as driver:
            with self.assertRaisesRegex(icloud.ICloudMailError, "均已使用"):
                registration.run_registration(None, "Test", on_email_acquired=callback)
        driver.assert_not_called()
        callback.assert_not_called()
        self.release.assert_not_called()

    def test_other_sources_mixed_sources_and_manual_email_keep_existing_flow(self):
        cases = [
            ("outlook", True, None),
            ("mailcode", True, None),
            ("icloud,outlook", True, None),
            ("outlook;icloud", True, None),
            (["icloud", "mailcode"], True, None),
            ("icloud", False, None),
            ("icloud", True, "fixed@icloud.com"),
            ("icloud", False, "fixed@icloud.com"),
        ]
        for source, automatic, email in cases:
            with self.subTest(source=source, automatic=automatic, email=email), patch.multiple(
                email_config, EMAIL_SOURCE=source, USE_EMAIL_SERVICE=automatic,
            ), patch("core.gptaccount_registration.run_gptaccount_registration") as driver:
                callback = Mock()
                registration.run_registration(email, "Test", on_email_acquired=callback)
                self.assertEqual(driver.call_args.kwargs["email"], email)
                self.assertIs(driver.call_args.kwargs["on_email_acquired"], callback)
                callback.assert_not_called()
        self.acquire.assert_not_called()
        self.release.assert_not_called()

    def test_roxy_startup_failure_recycles_fixed_suffix_for_retry(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "core.db._active_sqlite_path", return_value=Path(temp) / "icloud.sqlite3",
        ), patch("core.db._ensure_storage"), patch("core.db._ensure_sqlite"), patch.multiple(
            icloud_config, ICLOUD_EMAILS=["one@icloud.com"], ICLOUD_MODE="plus_once",
        ), patch.object(driver_config, "REGISTRATION_DRIVER", "roxy"), patch.object(
            roxy, "RoxyBrowserClient",
        ) as client:
            with closing(icloud.db._sqlite_conn()) as conn, conn:
                conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT NOT NULL, payload TEXT NOT NULL)")
            self.acquire.side_effect = email_provider.acquire_email_from_source
            self.release.side_effect = icloud.release_email_if_unconsumed
            client.return_value.open_profile.side_effect = RuntimeError("browser startup failed")
            acquired = []
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "browser startup failed"):
                    registration.run_registration(None, "Test", on_email_acquired=acquired.append)
            self.assertEqual(len(acquired), 2)
            self.assertEqual(acquired[0], acquired[1])
            self.assertRegex(acquired[0], r"^one\+[a-z0-9]+@icloud\.com$")
            self.assertEqual(self.release.call_count, 2)

    def test_callback_cancellation_releases_email_before_browser_start(self):
        callback = Mock(side_effect=StopRequested("用户手动停止"))
        with patch("core.gptaccount_registration.run_gptaccount_registration") as driver:
            with self.assertRaises(StopRequested):
                registration.run_registration(None, "Test", on_email_acquired=callback)
        driver.assert_not_called()
        self.release.assert_called_once_with("one+fixed@icloud.com", note="注册异常: 用户手动停止")

    def test_cleanup_error_does_not_mask_original_startup_error(self):
        self.release.side_effect = RuntimeError("cleanup failed")
        with patch(
            "core.gptaccount_registration.run_gptaccount_registration",
            side_effect=ValueError("startup failed"),
        ), self.assertLogs(registration.logger, level="WARNING"):
            with self.assertRaisesRegex(ValueError, "startup failed"):
                registration.run_registration(None, "Test")

    def test_handled_driver_failure_is_not_recycled_again(self):
        result = {"success": False, "email": "one+fixed@icloud.com", "error": "account unusable"}
        with patch("core.gptaccount_registration.run_gptaccount_registration", return_value=result):
            self.assertIs(registration.run_registration(None, "Test"), result)
        self.release.assert_not_called()

    def test_protocol_reuses_early_email_without_allocating_twice(self):
        callback = Mock()
        with patch.object(driver_config, "REGISTRATION_DRIVER", "protocol"), patch.object(
            registration, "acquire_email",
        ) as late_acquire, patch.object(
            registration, "BrowserSession", side_effect=RuntimeError("session startup failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "session startup failed"):
                registration.run_registration(None, "Test", on_email_acquired=callback)
        self.acquire.assert_called_once_with("icloud")
        callback.assert_called_once_with("one+fixed@icloud.com")
        late_acquire.assert_not_called()
        self.release.assert_called_once()


class DelayedEmailAllocationTests(unittest.TestCase):
    @patch("core.email_provider.acquire_email")
    def test_acquire_email_after_input_keeps_fixed_email_without_allocating(self, acquire):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            self.assertEqual(
                email_provider.acquire_email_after_input("fixed@example.com"),
                "fixed@example.com",
            )
        acquire.assert_not_called()

    @patch("core.email_provider.acquire_email", return_value="allocated@example.com")
    def test_acquire_email_after_input_allocates_only_for_automatic_mode(self, acquire):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(
                email_provider.acquire_email_after_input(None),
                "allocated@example.com",
            )
        acquire.assert_called_once_with()

    def test_roxy_finds_input_before_allocating_email(self):
        events = []
        email_input = object()

        def find_input(*args, **kwargs):
            events.append("find_input")
            return email_input

        def acquire():
            events.append("acquire_email")
            return "roxy@example.com"

        with patch.object(roxy, "_wait_for_email_input", side_effect=find_input), patch.object(
            roxy,
            "_human_type_text",
            side_effect=lambda *args, **kwargs: events.append("type_email"),
        ), patch.object(
            roxy,
            "_email_input_value_state",
            return_value={"inputs": [{"value": "roxy@example.com"}]},
        ), patch.object(
            roxy,
            "_submit_email_step",
            side_effect=lambda *args, **kwargs: events.append("submit_email"),
        ), patch.object(
            roxy,
            "_wait_email_submit_next_state",
            return_value="otp",
        ), patch.object(roxy, "human_delay"), patch.object(roxy, "_check_manual_stop"):
            result = roxy._submit_email_and_wait_next(
                object(), None, email_supplier=acquire
            )

        self.assertEqual(result, "otp")
        self.assertEqual(
            events,
            ["find_input", "acquire_email", "type_email", "submit_email"],
        )

    def test_roxy_accepts_verification_page_reached_during_slow_diagnostic(self):
        """复现邮箱提交超时后，读取诊断 DOM 时才完成 OTP 跳转的竞态。"""
        with patch.object(
            roxy, "_current_email_submit_next_state", side_effect=[None, "otp"]
        ), patch.object(roxy, "_type_email_address"), patch.object(
            roxy, "_email_input_value_state",
            side_effect=[{"inputs": [{"value": "slow@example.com"}]}, {"inputs": []}],
        ), patch.object(roxy, "_submit_email_step"), patch.object(
            roxy, "_wait_email_submit_next_state", return_value="unknown"
        ), patch.object(roxy, "human_delay"):
            result = roxy._submit_email_and_wait_next(
                object(), "slow@example.com", attempts=3
            )

        self.assertEqual(result, "otp")

    def test_browser_use_finds_input_before_allocating_email(self):
        events = []
        email_input = object()

        def find_input(*args, **kwargs):
            events.append("find_input")
            return email_input

        def acquire():
            events.append("acquire_email")
            return "browser@example.com"

        with patch.object(browser_use, "_wait_for_email_input_pw", side_effect=find_input), patch.object(
            browser_use,
            "_human_fill_locator",
            side_effect=lambda *args, **kwargs: events.append("type_email"),
        ), patch.object(
            browser_use,
            "_submit_email_step_pw",
            side_effect=lambda *args, **kwargs: events.append("submit_email") or True,
        ), patch.object(
            browser_use,
            "_wait_after_email_submit_transition",
            return_value="email_verification",
        ), patch.object(browser_use, "_human_pause"), patch.object(
            browser_use, "_check_manual_stop"
        ), patch.object(browser_use, "_page_url", return_value="https://chatgpt.com/auth/login"):
            result = browser_use._submit_email_until_transition(
                object(), object(), None, email_supplier=acquire
            )

        self.assertEqual(result, "email_verification")
        self.assertEqual(
            events,
            ["find_input", "acquire_email", "type_email", "submit_email"],
        )


if __name__ == "__main__":
    unittest.main()
