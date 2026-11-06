import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from config import email as cfg
from core import icloud_mail_client as icloud
from core import qqmail_client as qqmail


class ICloudImapTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(icloud.time, "time", return_value=1000.0))
        stack.enter_context(patch.object(icloud.time, "monotonic", side_effect=lambda: self.now))
        stack.enter_context(patch.object(icloud.time, "sleep", side_effect=self.advance))
        stack.enter_context(patch.object(icloud.logger, "warning"))
        stack.enter_context(patch.multiple(
            cfg, QQ_EMAIL="reader@example.com", QQ_IMAP_PASSWORD="test-password",
            QQ_IMAP_SERVER="imap.example.com", QQ_IMAP_PORT=993, IMAP_SOCKET_TIMEOUT=10,
        ))

    def advance(self, seconds):
        self.now += seconds

    def mail(self, failure=None):
        mail = MagicMock()
        folder = ["INBOX"]
        raw = (
            b"From: OpenAI <noreply@openai.com>\r\n"
            b"To: target@icloud.com\r\n"
            b"Date: Thu, 01 Jan 1970 00:16:40 +0000\r\n"
            b"Subject: Your ChatGPT code is 123456\r\n\r\n"
            b"Your verification code is 123456"
        )

        def fail_at(phase):
            if failure == phase:
                self.advance(mail.sock.settimeout.call_args.args[0])
                raise TimeoutError("read timed out")

        def select(name, **kwargs):
            fail_at("select")
            folder[0] = name
            return "OK", [b"1"]

        def search(*args):
            fail_at("search" if folder[0] == "INBOX" else "junk")
            return "OK", [b"1" if folder[0] == "INBOX" else b""]

        def fetch(mid, query):
            fail_at("header" if query == "(BODY.PEEK[HEADER])" else "body")
            return "OK", [(b"1 (BODY[]", raw), b")"]

        mail.login.side_effect = lambda *args: fail_at("login")
        mail.select.side_effect = select
        mail.search.side_effect = search
        mail.fetch.side_effect = fetch
        return mail

    def fetch(self, **kwargs):
        return icloud.fetch_latest_otp(
            "target@icloud.com", after_ts=1000.0, poll_interval=3,
            settle_seconds=0, **kwargs,
        )

    def test_read_timeout_reconnects_and_returns_existing_code(self):
        for phase in ("search", "header", "body"):
            with self.subTest(phase=phase):
                self.now = 1000.0
                first, second = self.mail(phase), self.mail()
                with patch.object(qqmail.imaplib, "IMAP4_SSL", side_effect=[first, second]) as connect, \
                        patch.object(icloud, "_latest_otp_in_folder", wraps=icloud._latest_otp_in_folder) as scan:
                    self.assertEqual(self.fetch(max_wait=20), "123456")
                self.assertEqual(self.now, 1013.0)
                self.assertEqual([c.kwargs["timeout"] for c in connect.call_args_list], [10, 7])
                self.assertTrue(all(c.args[2] == 1000.0 and c.args[4] == 1020.0
                                    for c in scan.call_args_list))
                for mail in (first, second):
                    mail.shutdown.assert_called_once()
                    mail.logout.assert_not_called()

    def test_login_or_initial_select_timeout_closes_partial_connection(self):
        for phase in ("login", "select"):
            with self.subTest(phase=phase):
                self.now = 1000.0
                mail = self.mail(phase)
                with patch.object(qqmail.imaplib, "IMAP4_SSL", return_value=mail):
                    with self.assertRaisesRegex(qqmail.QQMailClientError, "read timed out"):
                        qqmail._connect_imap(deadline=1002.0)
                self.assertEqual(self.now, 1002.0)
                mail.shutdown.assert_called_once()
                mail.logout.assert_not_called()

    def test_each_operation_reduces_timeout_to_remaining_budget(self):
        mail = self.mail()
        mail.login.side_effect = lambda *args: self.advance(3)
        with patch.object(qqmail.imaplib, "IMAP4_SSL", return_value=mail) as connect:
            self.assertIs(qqmail._connect_imap(deadline=1005.0), mail)
        connect.assert_called_once_with("imap.example.com", 993, timeout=5)
        self.assertEqual([c.args[0] for c in mail.sock.settimeout.call_args_list], [5, 2])

    def test_total_deadline_stops_retry_without_extra_sleep(self):
        mail = self.mail("header")
        with patch.object(qqmail.imaplib, "IMAP4_SSL", return_value=mail) as connect:
            with self.assertRaisesRegex(icloud.ICloudMailError, "超时（2s）"):
                self.fetch(max_wait=2)
        self.assertEqual(self.now, 1002.0)
        connect.assert_called_once()
        mail.shutdown.assert_called_once()
        self.assertTrue(all(c.args[0] == 2 for c in mail.sock.settimeout.call_args_list))

    def test_expired_budget_does_not_open_another_connection(self):
        with patch.object(qqmail.imaplib, "IMAP4_SSL") as connect:
            with self.assertRaisesRegex(qqmail.QQMailClientError, "等待时间已耗尽"):
                qqmail._connect_imap(deadline=1000.0)
        connect.assert_not_called()

    def test_junk_timeout_does_not_discard_code_already_read(self):
        mail = self.mail("junk")
        with patch.object(qqmail.imaplib, "IMAP4_SSL", return_value=mail) as connect:
            self.assertEqual(self.fetch(max_wait=20), "123456")
        connect.assert_called_once()
        mail.shutdown.assert_called_once()
        mail.logout.assert_not_called()

    def test_shared_qq_reader_retries_socket_timeout(self):
        first, second = self.mail("body"), self.mail()
        with patch.object(qqmail.imaplib, "IMAP4_SSL", side_effect=[first, second]), \
                patch.object(qqmail.logger, "warning"):
            self.assertEqual(qqmail.fetch_latest_otp(
                "target@icloud.com", after_ts=1000.0, max_wait=20,
                poll_interval=3, settle_seconds=0,
            ), "123456")
        for mail in (first, second):
            mail.shutdown.assert_called_once()
            mail.logout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
