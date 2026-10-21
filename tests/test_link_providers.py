import json
import unittest
from unittest.mock import MagicMock, patch

from config import link_providers as cfg
from core import extract_link_service, link_providers


def _sse(*events):
    lines = [b": ping", b""]
    for name, data in events:
        lines += [f"event: {name}".encode(), f"data: {json.dumps(data)}".encode(), b""]
    return lines


class _Resp:
    def __init__(self, lines, status=200):
        self.status_code = status
        self.text = ""
        self._lines = lines

    def iter_lines(self):
        return iter(self._lines)


class NerverUpiTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(cfg, NERVER_UPI_PROXY_POOL=["1.2.3.4:8080:user:pass"],
                                 NERVER_UPI_API_BASE="https://upi.example/", NERVER_UPI_TIMEOUT=60)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = patch.object(link_providers, "db").start()
        self.addCleanup(patch.stopall)

    def _run(self, resp):
        session = MagicMock()
        session.post.return_value = resp
        with patch.object(link_providers.curl_requests, "Session", return_value=session) as factory:
            try:
                return link_providers.run_nerver_upi(account_id=7, access_token="eyJ.tok.en"), session, factory
            finally:
                session.close.assert_called_once()

    def test_success_maps_to_upstream_fields_and_sends_proxy_as_param(self):
        done = {"ok": True, "request_id": "req_1", "upi_redirect_url": "https://pay/x",
                "upi_qr_data": "upi://pay?pa=x", "upi_qr_image_url": "https://qr/x.png", "next_action_type": "upi_qr"}
        result, session, factory = self._run(_Resp(_sse(
            ("hello", {"request_id": "req_1"}),
            ("progress", {"type": "step", "step": "checkout"}),
            ("done", done),
        )))
        factory.assert_called_once_with(trust_env=False)
        url = session.post.call_args.args[0]
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(url, "https://upi.example/api/v1/upi/stream")
        self.assertEqual(body, {"token": "eyJ.tok.en", "proxy": "http://user:pass@1.2.3.4:8080"})
        self.assertNotIn("proxies", session.post.call_args.kwargs)
        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "upi")
        self.assertEqual(result["job_id"], "req_1")
        self.assertEqual(result["result"]["long_url"], "https://pay/x")
        self.assertEqual(result["result"]["copy_paste"], "upi://pay?pa=x")
        self.assertEqual(result["result"]["image_url_png"], "https://qr/x.png")
        self.db.update_account_extract.assert_called_once()

    def test_failure_done_raises_with_reason(self):
        with self.assertRaisesRegex(RuntimeError, "token-invalidated"):
            self._run(_Resp(_sse(("done", {"ok": False, "reason": "token-invalidated", "error": "401"}))))

    def test_success_without_link_or_qr_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, "没有链接"):
            self._run(_Resp(_sse(("done", {"ok": True, "request_id": "req_2"}))))

    def test_http_error_and_missing_done(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            self._run(_Resp([], status=400))
        with self.assertRaisesRegex(RuntimeError, "未返回 done"):
            self._run(_Resp(_sse(("hello", {}))))

    def test_empty_proxy_pool_rejected(self):
        with patch.object(cfg, "NERVER_UPI_PROXY_POOL", []):
            with self.assertRaisesRegex(ValueError, "NERVER_UPI_PROXY_POOL"):
                link_providers.run_nerver_upi(account_id=1, access_token="t")


class ProviderDispatchTests(unittest.TestCase):
    def test_active_provider_selection(self):
        for value in ("", "cdk", "CDK"):
            with patch.object(cfg, "LINK_PROVIDER", value):
                self.assertIsNone(link_providers.active())
        with patch.object(cfg, "LINK_PROVIDER", "nerver_upi"):
            self.assertEqual(link_providers.active().name, "nerver_upi")
        with patch.object(cfg, "LINK_PROVIDER", "nope"), self.assertRaises(ValueError):
            link_providers.active()

    def test_enqueue_uses_provider_without_cdk(self):
        final = {"ok": True, "status": "success", "link_type": "upi", "result": {"long_url": "u"}}
        provider = link_providers.LinkProvider("fake", "upi", MagicMock(return_value=final))
        with patch.object(link_providers, "active", return_value=provider), \
                patch.object(extract_link_service, "db") as db, \
                patch.object(extract_link_service, "_create_extract_job") as create_job:
            db.claim_account_extract.return_value = True
            db.mark_account_extract_running.return_value = True
            queued = extract_link_service.enqueue_account_extract(account_id=3, email="a@b", access_token="tok")
            self.assertTrue(queued["accepted"])
            self.assertEqual(queued["future"].result(timeout=5), final)
        db.claim_account_extract.assert_called_once_with(3, trigger="manual", link_type="upi")
        provider.run.assert_called_once_with(account_id=3, access_token="tok")
        db.update_account_extract.assert_called_with(3, final)
        create_job.assert_not_called()

    def test_provider_failure_marks_account_failed(self):
        provider = link_providers.LinkProvider("fake", "upi", MagicMock(side_effect=RuntimeError("boom")))
        with patch.object(link_providers, "active", return_value=provider), \
                patch.object(extract_link_service, "db") as db:
            db.claim_account_extract.return_value = True
            db.mark_account_extract_running.return_value = True
            result = extract_link_service.enqueue_account_extract(
                account_id=4, email="a@b", access_token="tok")["future"].result(timeout=5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("boom", result["error"])
        self.assertEqual(db.update_account_extract.call_args.args[1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
