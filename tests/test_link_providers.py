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
    def __init__(self, lines=(), status=200, text=""):
        self.status_code = status
        self.text = text
        self._lines = lines

    def iter_lines(self):
        return iter(self._lines)

    def json(self):
        return json.loads(self.text)


class NerverUpiTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(cfg, NERVER_UPI_PROXY_POOL=["1.2.3.4:8080:user:pass"],
                                 NERVER_UPI_API_BASE="https://upi.example/", NERVER_UPI_TIMEOUT=60,
                                 NERVER_UPI_MODE="stream")
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
        self.assertEqual(session.post.call_args.kwargs["headers"], {"Accept": "text/event-stream"})
        self.assertTrue(session.post.call_args.kwargs["stream"])
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

    def test_json_success_matches_stream_result_and_sends_direct_request(self):
        done = {"ok": True, "request_id": "req_json", "upi_redirect_url": "https://pay/x",
                "upi_qr_data": "upi://pay?pa=x", "upi_qr_image_url": "https://qr/x.png",
                "next_action_type": "display_upi_qr_code"}
        stream_result, _, _ = self._run(_Resp(_sse(("done", done))))
        with patch.object(cfg, "NERVER_UPI_MODE", "json"):
            result, session, factory = self._run(_Resp(text=json.dumps(done)))
        self.assertEqual(result, stream_result)
        factory.assert_called_once_with(trust_env=False)
        session.post.assert_called_once_with(
            "https://upi.example/api/v1/upi",
            json={"token": "eyJ.tok.en", "proxy": "http://user:pass@1.2.3.4:8080"},
            headers={"Accept": "application/json"}, timeout=(15, 60), stream=False,
        )
        self.db.update_account_extract.assert_not_called()

    def test_both_modes_accept_hosted_url_or_qr_only(self):
        for mode in ("stream", "json"):
            for field, value, mapped in (
                ("upi_hosted_url", "https://pay/hosted", "long_url"),
                ("upi_qr_data", "upi://pay?pa=x", "copy_paste"),
                ("upi_qr_image_url", "https://qr/x.png", "image_url_png"),
            ):
                with self.subTest(mode=mode, field=field), patch.object(cfg, "NERVER_UPI_MODE", mode):
                    done = {"ok": True, field: value}
                    resp = _Resp(_sse(("done", done))) if mode == "stream" else _Resp(text=json.dumps(done))
                    result, _, _ = self._run(resp)
                    self.assertEqual(result["result"][mapped], value)

    def test_json_business_failure_preserves_reason_message_and_error(self):
        done = {"ok": False, "reason": "token-invalidated", "message": "token 已失效", "error": "401"}
        with patch.object(cfg, "NERVER_UPI_MODE", "json"), \
                self.assertRaisesRegex(RuntimeError, "token-invalidated token 已失效；401"):
            self._run(_Resp(text=json.dumps(done)))

    def test_json_success_without_link_or_qr_is_failure(self):
        with patch.object(cfg, "NERVER_UPI_MODE", "json"), self.assertRaisesRegex(RuntimeError, "没有链接"):
            self._run(_Resp(text=json.dumps({"ok": True, "request_id": "empty_json"})))

    def test_json_http_error_preserves_status_and_body(self):
        with patch.object(cfg, "NERVER_UPI_MODE", "json"), \
                self.assertRaisesRegex(RuntimeError, "HTTP 503: upstream unavailable"):
            self._run(_Resp(status=503, text="upstream unavailable"))

    def test_json_malformed_or_non_object_response_is_failure(self):
        for body, error in (("<html>bad gateway</html>", "无效 JSON"), ("", "无效 JSON"),
                            ("null", "预期 JSON 对象"), ("[]", "预期 JSON 对象")):
            with self.subTest(body=body), patch.object(cfg, "NERVER_UPI_MODE", "json"), \
                    self.assertRaisesRegex(RuntimeError, error):
                self._run(_Resp(text=body))

    def test_invalid_mode_rejected_before_request(self):
        with patch.object(cfg, "NERVER_UPI_MODE", "nope"), \
                patch.object(link_providers.curl_requests, "Session") as factory, \
                self.assertRaisesRegex(ValueError, "NERVER_UPI_MODE"):
            link_providers.run_nerver_upi(account_id=7, access_token="t")
        factory.assert_not_called()

    def test_timeout_closes_session_without_retry_in_both_modes(self):
        for mode in ("stream", "json"):
            with self.subTest(mode=mode), patch.object(cfg, "NERVER_UPI_MODE", mode), \
                    patch.object(link_providers.curl_requests, "Session") as factory:
                session = factory.return_value
                session.post.side_effect = link_providers.curl_requests.exceptions.Timeout("timeout")
                with self.assertRaises(link_providers.curl_requests.exceptions.Timeout):
                    link_providers.run_nerver_upi(account_id=7, access_token="t")
                session.post.assert_called_once()
                session.close.assert_called_once()


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
