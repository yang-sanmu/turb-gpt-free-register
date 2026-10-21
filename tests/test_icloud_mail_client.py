import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import icloud as cfg
from core import email_provider
from core import icloud_mail_client as client

# Real header shapes from iCloud-forwarded OpenAI mails (address anonymised).
TO = "Hide My Email <foam.base@icloud.com>"
VERP_A = "bounces+20216706-738d-foam.base+kj59aaf=icloud.com"
VERP_B = "bounces+20216706-738d-foam.base+zz00abc=icloud.com"
VERP_BASE = "bounces+20216706-e93a-foam.base=icloud.com"


class AddressMatchTests(unittest.TestCase):
    def test_plus_address_matches_only_its_own_verp(self):
        self.assertTrue(client.is_addressed_to(VERP_A, TO, "foam.base+kj59aaf@icloud.com"))
        self.assertFalse(client.is_addressed_to(VERP_B, TO, "foam.base+kj59aaf@icloud.com"))
        # To alone is shared by every parallel task on this base address.
        self.assertFalse(client.is_addressed_to(TO, TO, "foam.base+kj59aaf@icloud.com"))

    def test_direct_address_ignores_mail_for_plus_children(self):
        self.assertTrue(client.is_addressed_to(VERP_BASE, TO, "foam.base@icloud.com"))
        self.assertTrue(client.is_addressed_to(TO, TO, "foam.base@icloud.com"))
        self.assertFalse(client.is_addressed_to(VERP_A, TO, "foam.base@icloud.com"))

    def test_folded_header_whitespace_and_prefix_names(self):
        folded = "smtp.mailfrom=<bounce+4cd5c8.b462b7-foam.base+kj59aaf=icloud.com_at_tm1_openai_com@icloud.com>"
        self.assertTrue(client.is_addressed_to(folded.replace("+kj", "+k j"), TO, "foam.base+kj59aaf@icloud.com"))
        # "xfoam.base=icloud.com" must not count as "foam.base".
        self.assertFalse(client.is_addressed_to("bounces+1-2-xfoam.base=icloud.com", "", "foam.base@icloud.com"))


class PickTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = Path(temp.name) / "icloud.sqlite3"
        patcher = patch("core.db._active_sqlite_path", return_value=db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("_ensure_storage", None),):
            p = patch(f"core.db.{name}", return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch("core.db.get_account_by_email", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_plus_mode_round_robins_with_unique_suffix(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["A.one@icloud.com", "b.two+old@icloud.com"], ICLOUD_MODE="plus", ICLOUD_PLUS_LENGTH=7):
            picked = [client.pick_email() for _ in range(6)]
        self.assertEqual(len(set(picked)), 6)
        for addr in picked:
            local, domain = addr.split("@")
            base, suffix = local.split("+")
            self.assertIn(base, {"a.one", "b.two"})
            self.assertEqual(domain, "icloud.com")
            self.assertRegex(suffix, r"^[a-z0-9]{7}$")
        self.assertEqual({a.split("+")[0] for a in picked}, {"a.one", "b.two"})
        self.assertTrue(client.is_known_email(picked[0]))

    def test_direct_mode_uses_each_once_and_release_returns_it(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["one@icloud.com", "two@icloud.com"], ICLOUD_MODE="direct"):
            first, second = client.pick_email(), client.pick_email()
            self.assertEqual({first, second}, {"one@icloud.com", "two@icloud.com"})
            with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                client.pick_email()
            client.release_email(first, status="available")
            self.assertEqual(client.pick_email(), first)

    def test_empty_list_and_bad_mode(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=[], ICLOUD_MODE="plus"):
            with self.assertRaisesRegex(client.ICloudMailError, "ICLOUD_EMAILS"):
                client.pick_email()
        with patch.multiple(cfg, ICLOUD_EMAILS=["a@icloud.com"], ICLOUD_MODE="nope"):
            with self.assertRaisesRegex(client.ICloudMailError, "ICLOUD_MODE"):
                client.pick_email()


class ProviderDispatchTests(unittest.TestCase):
    def test_source_is_valid_and_routes_to_icloud_client(self):
        self.assertEqual(email_provider.parse_email_sources("icloud"), ["icloud"])
        with patch.object(client, "pick_email", return_value="x+abc@icloud.com") as pick:
            self.assertEqual(email_provider.acquire_email_from_source("icloud"), "x+abc@icloud.com")
        pick.assert_called_once()
        with patch.object(email_provider, "_registered_email_source", return_value=None), \
                patch.object(client, "is_known_email", return_value=True), \
                patch.object(client, "fetch_latest_otp", return_value="123456") as fetch, \
                patch("config.email.USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("x+abc@icloud.com", after_ts=1.0), "123456")
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
