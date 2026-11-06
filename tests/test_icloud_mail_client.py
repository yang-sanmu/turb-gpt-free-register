import importlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from pathlib import Path
from threading import Barrier, BrokenBarrierError, local
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
        for name, value in (("_CLAIMS", {}), ("_CLAIM_CONTEXT", local())):
            p = patch.object(client, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("_ensure_storage", None), ("_ensure_sqlite", None)):
            p = patch(f"core.db.{name}", return_value=value)
            p.start()
            self.addCleanup(p.stop)
        with closing(client.db._sqlite_conn()) as conn, conn:
            conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT NOT NULL, payload TEXT NOT NULL)")

    def save_account(self, email):
        with closing(client.db._sqlite_conn()) as conn, conn:
            conn.execute("INSERT INTO accounts(email, payload) VALUES(?,?)", (email, json.dumps({"email": email})))

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

    def test_plus_once_uses_each_base_once_and_exhausts(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["ONE@icloud.com", "one+old@icloud.com", "two@icloud.com"], ICLOUD_MODE="plus_once", ICLOUD_PLUS_LENGTH=7):
            first, second = client.pick_email(), client.pick_email()
            self.assertRegex(first, r"^one\+[a-z0-9]{7}@icloud\.com$")
            self.assertRegex(second, r"^two\+[a-z0-9]{7}@icloud\.com$")
            with self.assertRaisesRegex(client.ICloudMailError, "2 个邮箱均已使用"):
                client.pick_email()
            self.assertTrue(client.is_known_email(first))
            client.release_email(first, status="available")
            self.assertEqual(client.pick_email(), first)
            with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                client.pick_email()

    def test_stale_cleanup_cannot_release_or_disable_a_new_claim(self):
        for mode in ("direct", "plus_once"):
            for first_cleanup in ("driver", "fallback"):
                with self.subTest(mode=mode, first_cleanup=first_cleanup), patch.multiple(
                    cfg, ICLOUD_EMAILS=[f"{mode}-{first_cleanup}@icloud.com"], ICLOUD_MODE=mode,
                ), ThreadPoolExecutor(max_workers=1) as old_worker, ThreadPoolExecutor(max_workers=1) as new_worker:
                    first = old_worker.submit(client.pick_email).result(timeout=10)
                    release = client.release_email if first_cleanup == "driver" else client.release_email_if_unconsumed
                    old_worker.submit(release, first).result(timeout=10)
                    second = new_worker.submit(client.pick_email).result(timeout=10)
                    self.assertEqual(second, first)

                    # A 已释放，B 已重新领取；A 的外层兜底和迟到的停用都不能影响 B。
                    stale_email = f" {first.upper()} "
                    self.assertFalse(old_worker.submit(
                        client.release_email_if_unconsumed, stale_email, note="旧任务兜底",
                    ).result(timeout=10))
                    for status in ("available", "failed", "disabled"):
                        old_worker.submit(
                            client.release_email, stale_email, status=status, note="旧任务状态写入",
                        ).result(timeout=10)
                        with closing(client._conn()) as conn:
                            row = conn.execute(f"SELECT status, note FROM {client._TABLE} WHERE email=?", (second,)).fetchone()
                        self.assertEqual(tuple(row), ("used", None))
                        with self.assertRaises(client.ICloudMailError):
                            client.pick_email()

                    # B 仍能正常回收；线程池复用同一线程时，下次领取也必须可回收。
                    self.assertTrue(new_worker.submit(client.release_email_if_unconsumed, second).result(timeout=10))
                    retried = old_worker.submit(client.pick_email).result(timeout=10)
                    self.assertEqual(retried, first)
                    self.assertTrue(old_worker.submit(client.release_email_if_unconsumed, retried).result(timeout=10))

    def test_untracked_caller_preserves_existing_recovery_and_disable(self):
        for mode in ("direct", "plus_once", "plus"):
            with self.subTest(mode=mode), patch.multiple(
                cfg, ICLOUD_EMAILS=[f"untracked-{mode}@icloud.com"], ICLOUD_MODE=mode,
            ):
                with ThreadPoolExecutor(max_workers=1) as owner:
                    email = owner.submit(client.pick_email).result(timeout=10)
                # 原有调用方可在线程结束后回收，不要求必须从领取线程调用。
                self.assertTrue(client.release_email_if_unconsumed(email))
                client.release_email(email, status="disabled", note="正常停用")
                with closing(client._conn()) as conn:
                    row = conn.execute(f"SELECT status, note FROM {client._TABLE} WHERE email=?", (email,)).fetchone()
                self.assertEqual(tuple(row), ("disabled", "正常停用"))

    def test_owner_can_disable_after_release_until_someone_reclaims(self):
        for mode in ("direct", "plus_once"):
            with self.subTest(mode=mode), patch.multiple(
                cfg, ICLOUD_EMAILS=[f"disable-{mode}@icloud.com"], ICLOUD_MODE=mode,
            ):
                email = client.pick_email()
                client.release_email(email)
                client.release_email(email, status="disabled", note="外层判定需停用")
                self.assertFalse(client.release_email_if_unconsumed(email))
                with closing(client._conn()) as conn:
                    row = conn.execute(f"SELECT status, note FROM {client._TABLE} WHERE email=?", (email,)).fetchone()
                self.assertEqual(tuple(row), ("disabled", "外层判定需停用"))
                with self.assertRaises(client.ICloudMailError):
                    client.pick_email()

    def test_new_manual_email_job_does_not_inherit_previous_claim(self):
        from core import registration_service as service

        def in_job(job_id, action):
            service._THREAD_CTX.job_id = job_id
            try:
                return action()
            finally:
                del service._THREAD_CTX.job_id

        def claim_and_release():
            email = client.pick_email()
            client.release_email(email)
            return email

        for mode in ("direct", "plus_once"):
            with self.subTest(mode=mode), patch.multiple(
                cfg, ICLOUD_EMAILS=[f"manual-retry-{mode}@icloud.com"], ICLOUD_MODE=mode,
            ), patch.object(service, "_THREAD_CTX", local()), patch.object(
                email_provider, "resolve_email_source", return_value="icloud",
            ), ThreadPoolExecutor(max_workers=1) as old_worker, ThreadPoolExecutor(max_workers=1) as new_worker:
                email = old_worker.submit(in_job, 101, claim_and_release).result(timeout=10)
                self.assertEqual(new_worker.submit(in_job, 102, client.pick_email).result(timeout=10), email)
                # 仍属于旧任务的迟到回收必须被拒绝。
                self.assertFalse(old_worker.submit(
                    in_job, 101, lambda: client.release_email_if_unconsumed(email),
                ).result(timeout=10))
                self.assertTrue(new_worker.submit(
                    in_job, 102, lambda: client.release_email_if_unconsumed(email),
                ).result(timeout=10))
                # 同一线程的新任务手动指定邮箱，不经过 pick_email，也应能正常停用。
                self.assertTrue(old_worker.submit(
                    in_job, 103, lambda: service._disable_job_email(email, "手动邮箱任务失败"),
                ).result(timeout=10))
                with closing(client._conn()) as conn:
                    row = conn.execute(f"SELECT status FROM {client._TABLE} WHERE email=?", (email,)).fetchone()
                self.assertEqual(row[0], "disabled")

    def test_persisted_claim_without_process_owner_keeps_legacy_recovery(self):
        client.release_email("persisted@icloud.com", status="used")
        self.assertTrue(client.release_email_if_unconsumed("persisted@icloud.com"))
        self.assertFalse(client.release_email_if_unconsumed("persisted@icloud.com"))

    def test_plus_once_reuses_suffix_after_reload_and_length_change(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["one@icloud.com"], ICLOUD_MODE="plus_once", ICLOUD_PLUS_LENGTH=7):
            first = client.pick_email()
            client.release_email(first)
            importlib.reload(client)
            with patch.object(cfg, "ICLOUD_PLUS_LENGTH", 12), \
                    patch.object(client.random, "choices", side_effect=AssertionError("固定后缀不应重新生成")):
                for _ in range(3):
                    self.assertEqual(client.pick_email(), first)
                    client.release_email(first)

    def test_plus_once_provider_recycles_failure_but_not_registered_email(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["one@icloud.com"], ICLOUD_MODE="plus_once"), \
                patch.object(email_provider, "resolve_email_source", return_value="icloud"):
            first = email_provider.acquire_email_from_source("icloud")
            self.assertTrue(email_provider.release_email_if_unconsumed(first, note="注册失败"))
            self.assertEqual(email_provider.acquire_email_from_source("icloud"), first)
            self.save_account(first)
            self.assertFalse(email_provider.release_email_if_unconsumed(first))
            with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                client.pick_email()

    def test_plus_once_provider_only_reclaims_used_email(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["one@icloud.com"], ICLOUD_MODE="plus_once"), \
                patch.object(email_provider, "resolve_email_source", return_value="icloud"):
            first = client.pick_email()
            for status in ("available", "failed", "disabled"):
                with self.subTest(status=status):
                    client.release_email(first, status=status, note="保留原状态")
                    self.assertFalse(email_provider.release_email_if_unconsumed(first, note="任务兜底回收"))
                    with closing(client._conn()) as conn:
                        row = conn.execute(f"SELECT status, note FROM {client._TABLE} WHERE email=?", (first,)).fetchone()
                    self.assertEqual(tuple(row), (status, "保留原状态"))
                    if status != "available":
                        with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                            client.pick_email()
            self.assertFalse(email_provider.release_email_if_unconsumed("unknown@icloud.com"))
            self.assertFalse(client.is_known_email("unknown@icloud.com"))

    def test_plus_once_skips_registered_alias_even_if_released(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["one@icloud.com", "two@icloud.com"], ICLOUD_MODE="plus_once"):
            first = client.pick_email()
            client.release_email(first)
            self.save_account(first)
            self.assertTrue(client.pick_email().startswith("two+"))
            with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                client.pick_email()

    def test_plus_once_ignores_consumed_or_registered_bare_base(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["used@icloud.com", "saved@icloud.com", "free@icloud.com"], ICLOUD_MODE="plus_once"):
            client.release_email("used@icloud.com", status="used")
            self.save_account("saved@icloud.com")
            self.assertTrue(client.pick_email().startswith("used+"))
            self.assertTrue(client.pick_email().startswith("saved+"))
            self.assertTrue(client.pick_email().startswith("free+"))
            with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                client.pick_email()

    def test_plus_once_skips_other_registered_suffix_before_generating(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["aaa@icloud.com", "bbb@icloud.com"], ICLOUD_MODE="plus_once"):
            self.save_account("AAA+cc@ICLOUD.COM")
            with patch.object(client.random, "choices", return_value=list("bbbbbbb")) as choices:
                self.assertEqual(client.pick_email(), "bbb+bbbbbbb@icloud.com")
                with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                    client.pick_email()
            choices.assert_called_once()

    def test_plus_once_skips_other_registered_suffix_on_retry(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["aaa@icloud.com"], ICLOUD_MODE="plus_once"):
            first = client.pick_email()
            client.release_email(first)
            self.save_account("aaa+old@icloud.com")
            with patch.object(client.random, "choices", side_effect=AssertionError("不可另换后缀")):
                with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                    client.pick_email()

    def test_plus_once_skips_unavailable_child_from_another_suffix(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["aaa@icloud.com"], ICLOUD_MODE="plus_once"):
            for status in ("used", "failed", "disabled"):
                with self.subTest(status=status):
                    client.release_email("aaa+old@icloud.com", status=status)
                    with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                        client.pick_email()

    def test_plus_once_matches_entire_base_and_domain(self):
        for email in ("xaaa+cc@icloud.com", "aaa2+cc@icloud.com", "aaa+cc@me.com", "aaa+cc@icloud.com.other"):
            self.save_account(email)
        with patch.multiple(cfg, ICLOUD_EMAILS=["aaa@icloud.com"], ICLOUD_MODE="plus_once"):
            self.assertTrue(client.pick_email().startswith("aaa+"))

    def test_direct_mode_matches_exact_address_only(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["saved@icloud.com", "aaa@icloud.com"], ICLOUD_MODE="direct"):
            self.save_account("saved@icloud.com")
            self.save_account("aaa+cc@icloud.com")
            client.release_email("aaa+old@icloud.com", status="used")
            self.assertEqual(client.pick_email(), "aaa@icloud.com")
            with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                client.pick_email()

    def test_plus_once_respects_unavailable_statuses(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["one@icloud.com"], ICLOUD_MODE="plus_once"):
            first = client.pick_email()
            for status in ("used", "failed", "disabled"):
                with self.subTest(status=status):
                    client.release_email(first, status=status)
                    with self.assertRaisesRegex(client.ICloudMailError, "均已使用"):
                        client.pick_email()

    def test_plus_once_parallel_claims_never_duplicate_a_base(self):
        def pick_or_none(_):
            try:
                return client.pick_email()
            except client.ICloudMailError:
                return None

        with patch.multiple(cfg, ICLOUD_EMAILS=[f"box{i}@icloud.com" for i in range(4)], ICLOUD_MODE="plus_once"):
            with ThreadPoolExecutor(max_workers=8) as pool:
                picked = [addr for addr in pool.map(pick_or_none, range(16)) if addr]
            self.assertEqual(len(picked), 4)
            self.assertEqual({addr.split("+")[0] for addr in picked}, {f"box{i}" for i in range(4)})
            client.release_email(picked[0])
            with ThreadPoolExecutor(max_workers=8) as pool:
                retried = [addr for addr in pool.map(pick_or_none, range(16)) if addr]
            self.assertEqual(retried, [picked[0]])

    def test_plus_once_claim_is_atomic_without_a_shared_process_lock(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["one@icloud.com"], ICLOUD_MODE="plus_once"):
            first = client.pick_email()
            client.release_email(first)
            self.save_account("other+saved@icloud.com")
            readers = Barrier(2)
            plus_base = client._plus_base

            def synchronize_readers(addr):
                if addr == "other+saved@icloud.com":
                    # 旧逻辑会让两个调度器都先读到 available；事务保护后后者等待前者提交。
                    try:
                        readers.wait(timeout=1)
                    except BrokenBarrierError:
                        pass
                return plus_base(addr)

            def pick_or_none(_):
                try:
                    return client.pick_email()
                except client.ICloudMailError:
                    return None

            # 独立进程不共享 db._LOCK；保留真实 SQLite 连接验证数据库层的互斥。
            with patch.object(client.db, "_LOCK", nullcontext()), \
                    patch.object(client, "_plus_base", side_effect=synchronize_readers):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    picked = list(pool.map(pick_or_none, range(2)))
            self.assertEqual([addr for addr in picked if addr], [first])

    def test_plus_once_keeps_its_mapping_when_switching_modes(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["one@icloud.com"], ICLOUD_MODE="plus_once"):
            first = client.pick_email()
            client.release_email(first)
            with patch.object(cfg, "ICLOUD_MODE", "plus"):
                other = client.pick_email()
                self.assertNotEqual(other, first)
                client.release_email(other)
            self.assertEqual(client.pick_email(), first)

    def test_plus_once_avoids_existing_available_usage_collision(self):
        with patch.multiple(cfg, ICLOUD_EMAILS=["one@icloud.com"], ICLOUD_MODE="plus_once", ICLOUD_PLUS_LENGTH=7):
            client.release_email("one+aaaaaaa@icloud.com")
            with patch.object(client.random, "choices", side_effect=[list("aaaaaaa"), list("bbbbbbb")]):
                self.assertEqual(client.pick_email(), "one+bbbbbbb@icloud.com")

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


class OtpWaitTests(unittest.TestCase):
    """iCloud forwarding lags 1.5-2.5 min; the default wait must cover it."""

    def _waited(self, *, otp_max_wait, icloud_wait, max_wait=None):
        clock = {"now": 1000.0}
        with patch.object(client.time, "time", side_effect=lambda: clock["now"]), \
                patch.object(client.time, "monotonic", side_effect=lambda: clock["now"]), \
                patch.object(client.time, "sleep", side_effect=lambda s: clock.update(now=clock["now"] + s)), \
                patch.object(client, "_connect_imap", side_effect=OSError("offline")), \
                patch("config.email.OTP_MAX_WAIT", otp_max_wait), \
                patch("config.email.OTP_POLL_INTERVAL", 10), \
                patch.object(cfg, "ICLOUD_OTP_MAX_WAIT", icloud_wait, create=True), \
                self.assertLogs(client.logger, level="INFO"):
            with self.assertRaisesRegex(client.ICloudMailError, r"超时（(\d+)s）") as ctx:
                client.fetch_latest_otp("x+abc@icloud.com", after_ts=1.0, max_wait=max_wait)
        return int(ctx.exception.args[0].split("（")[1].split("s")[0])

    def test_default_wait_uses_the_longer_icloud_setting(self):
        self.assertEqual(self._waited(otp_max_wait=90, icloud_wait=180), 180)
        self.assertEqual(self._waited(otp_max_wait=300, icloud_wait=180), 300)

    def test_explicit_max_wait_is_respected(self):
        self.assertEqual(self._waited(otp_max_wait=90, icloud_wait=180, max_wait=20), 20)


if __name__ == "__main__":
    unittest.main()
