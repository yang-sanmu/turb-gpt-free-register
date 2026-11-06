"""真实页面分支的隔离回归：不请求登录、不读取生产账号。"""
import unittest
from unittest.mock import MagicMock, patch

from core import account_liveness as login


class LoginStepTests(unittest.TestCase):
    callback = 'https://chatgpt.com/api/auth/callback/openai?code=fixture'
    mfa = {'page': {'type': 'mfa_challenge', 'payload': {'factor_id': 'factor'}},
           'continue_url': 'https://auth.openai.com/mfa-challenge/factor'}

    def setUp(self):
        self.session = MagicMock()
        self.session.get_auth_headers.return_value = {}
        self.session.post.return_value.status_code = 200
        self.session.post.return_value.json.return_value = {
            'page': {'type': 'email_otp_verification'},
            'continue_url': 'https://auth.openai.com/email-verification',
        }
        self.events = []
        self.mocks = {}
        for name, value in {
            '_account_registration_password': '', '_account_totp_secret': 'test-secret',
            '_account_totp_code': '123456', '_trigger_reauth_with_retry': 'https://auth.openai.com/authorize',
            '_follow_reauth_with_retry': 'https://auth.openai.com/email-verification',
            '_validate_reauth_with_retry': self.callback,
            '_validate_with_retry': {'continue_url': self.callback},
            '_mfa_issue_challenge': {}, '_mfa_verify': {'continue_url': self.callback},
            '_follow_continue_and_fetch': {'accessToken': 'fresh'}, 'human_delay': None,
            'fetch_session': {'accessToken': 'fresh'},
        }.items():
            p = patch.object(login, name, return_value=value)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        p = patch.object(login, 'send_email_otp', side_effect=lambda s: self.events.append('send'))
        self.send = p.start()
        self.addCleanup(p.stop)
        for name, value in {'request_sentinel_token': {}, 'build_sentinel_header': ('sentinel', None)}.items():
            p = patch('core.openai_auth.' + name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_email_otp_then_mfa_completes_before_fetching_token(self):
        self.mocks['_validate_with_retry'].return_value = self.mfa
        self.mocks['_mfa_verify'].side_effect = lambda *a: self.events.append('mfa') or {'continue_url': self.callback}
        self.mocks['_follow_continue_and_fetch'].side_effect = lambda *a, **k: self.events.append('session') or {'accessToken': 'fresh'}
        result = login._login_from_authorize(self.session, 'test@example.com', 'https://auth.openai.com/email-verification', 1)
        self.assertEqual(result['accessToken'], 'fresh')
        self.assertEqual(self.events, ['send', 'mfa', 'session'])
        self.mocks['_mfa_verify'].assert_called_once_with(self.session, 'factor', '123456')

    def test_missing_local_mfa_secret_is_verification_failure(self):
        self.mocks['_validate_with_retry'].return_value = self.mfa
        self.mocks['_account_totp_secret'].return_value = ''
        with self.assertRaisesRegex(RuntimeError, '缺少 totp_secret'):
            login._login_via_email_otp(self.session, 'test@example.com', 1)
        self.mocks['_follow_continue_and_fetch'].assert_not_called()

    def test_reauth_password_page_does_not_blindly_wait_for_mail(self):
        self.mocks['_follow_reauth_with_retry'].return_value = 'https://auth.openai.com/log-in/password'
        with patch.object(login, '_login_via_password_or_otp', return_value={'accessToken': 'pw'}) as password:
            result = login._login_via_reauth(self.session, 'test@example.com', 1)
        self.assertEqual(result['accessToken'], 'pw')
        password.assert_called_once()
        self.mocks['_validate_reauth_with_retry'].assert_not_called()
        self.send.assert_not_called()

    def test_reauth_requests_code_and_handles_mfa_after_email(self):
        self.mocks['_validate_reauth_with_retry'].side_effect = lambda *a, **k: self.events.append('wait') or self.mfa['continue_url']
        result = login._login_via_reauth(self.session, 'test@example.com', 1)
        self.assertEqual(result['accessToken'], 'fresh')
        self.assertEqual(self.events, ['send', 'wait'])
        self.mocks['_mfa_verify'].assert_called_once()

    def test_send_failure_does_not_wait(self):
        self.send.side_effect = RuntimeError('发送失败')
        with self.assertRaisesRegex(RuntimeError, '发送失败'):
            login._login_via_reauth(self.session, 'test@example.com', 1)
        self.mocks['_validate_reauth_with_retry'].assert_not_called()

    def test_password_mfa_path_still_works(self):
        self.mocks['_account_registration_password'].return_value = 'test-password'
        with patch.object(login, '_password_verify', return_value=self.mfa):
            result = login._login_from_authorize(self.session, 'test@example.com', 'https://auth.openai.com/log-in/password', 1)
        self.assertEqual(result['accessToken'], 'fresh')
        self.send.assert_not_called()
        self.session.post.assert_not_called()
        self.mocks['_mfa_verify'].assert_called_once()

    def test_passwordless_without_mfa_can_complete(self):
        self.mocks['_account_totp_secret'].return_value = ''
        result = login._login_from_authorize(self.session, 'test@example.com', 'https://auth.openai.com/log-in/password', 1)
        self.assertEqual(result['accessToken'], 'fresh')
        self.send.assert_not_called()
        self.assertEqual(self.session.post.call_args.args[0],
                         'https://auth.openai.com/api/accounts/passwordless/send-otp')
        self.mocks['_mfa_verify'].assert_not_called()

    def test_passwordless_email_then_mfa_still_completes(self):
        self.mocks['_validate_with_retry'].return_value = self.mfa
        result = login._login_from_authorize(self.session, 'test@example.com',
                                             'https://auth.openai.com/log-in/password', 1)
        self.assertEqual(result['accessToken'], 'fresh')
        self.mocks['_mfa_verify'].assert_called_once_with(self.session, 'factor', '123456')
        self.send.assert_not_called()

    def test_passwordless_failure_never_waits_for_mail(self):
        responses = [
            (200, {'page': {'type': 'login_password'}}),
            (200, {'error': {'code': 'invalid_auth_step'}}),
            (200, {'page': {'type': 'email_otp_verification'}, 'continue_url': 'https://auth.openai.com/error'}),
            (302, {'page': {'type': 'email_otp_verification'}}),
            (200, {}),
            (200, None),
        ]
        for status, body in responses:
            with self.subTest(status=status, body=body):
                self.session.post.return_value.status_code = status
                self.session.post.return_value.json.return_value = body
                with self.assertRaisesRegex(RuntimeError, '未进入验证码步骤'):
                    login._login_via_password_or_otp(self.session, 'test@example.com', 1)
        self.session.post.return_value.json.side_effect = ValueError('HTML error page')
        with self.assertRaisesRegex(RuntimeError, '未返回有效的验证码步骤'):
            login._login_via_password_or_otp(self.session, 'test@example.com', 1)
        self.session.post.return_value.raise_for_status.side_effect = RuntimeError('HTTP 400')
        with self.assertRaisesRegex(RuntimeError, 'HTTP 400'):
            login._login_via_password_or_otp(self.session, 'test@example.com', 1)
        self.mocks['_validate_with_retry'].assert_not_called()
        self.send.assert_not_called()

    def test_passwordless_request_preserves_mail_source_and_send_time(self):
        with patch('core.openai_auth.request_sentinel_token', return_value={}) as sentinel, \
                patch('core.openai_auth.build_sentinel_header', return_value=('token', 'observer')), \
                patch.object(login.time, 'time', return_value=1234):
            login._login_via_passwordless_otp(self.session, 'test@example.com', email_source='icloud')
        sentinel.assert_called_once_with(self.session, 'authorize_continue')
        self.session.get_auth_headers.assert_called_once_with(referer='https://auth.openai.com/log-in/password')
        request = self.session.post.call_args.kwargs
        self.assertFalse(request['allow_redirects'])
        self.assertEqual(request['headers']['openai-sentinel-token'], 'token')
        self.assertEqual(request['headers']['openai-sentinel-so-token'], 'observer')
        self.assertNotIn('data', request)
        self.mocks['_validate_with_retry'].assert_called_once_with(
            self.session, 'test@example.com', 1234, email_source='icloud')

    def test_unrecognized_page_never_waits_or_fetches_session(self):
        for path in ('/log-in', '/phone-verification', '/about-you', '/mfa-challenge', '/unknown'):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                login._login_from_authorize(self.session, 'test@example.com', 'https://auth.openai.com' + path, 1)
        self.send.assert_not_called()
        self.mocks['_follow_continue_and_fetch'].assert_not_called()

    def test_authorize_callback_does_not_force_password_or_otp(self):
        result = login._login_from_authorize(self.session, 'test@example.com', self.callback, 1)
        self.assertEqual(result['accessToken'], 'fresh')
        self.send.assert_not_called()

    def test_auth_continuation_is_followed_after_password_email_and_mfa(self):
        continuation = 'https://auth.openai.com/authorize/continue?state=fixture'
        for stage in ('password', 'email', 'mfa'):
            with self.subTest(stage=stage):
                self.mocks['_follow_continue_and_fetch'].reset_mock()
                self.mocks['_validate_with_retry'].return_value = {'continue_url': continuation}
                self.mocks['_mfa_verify'].return_value = {'continue_url': continuation}
                if stage == 'password':
                    self.mocks['_account_registration_password'].return_value = 'fixture'
                    with patch.object(login, '_password_verify', return_value={'continue_url': continuation}):
                        result = login._login_via_password_or_otp(self.session, 'test@example.com', 1)
                elif stage == 'email':
                    result = login._login_via_email_otp(self.session, 'test@example.com', 1)
                else:
                    result = login._finish_login_result(self.session, 'test@example.com', self.mfa, 1)
                self.assertEqual(result['accessToken'], 'fresh')
                self.assertEqual(self.mocks['_follow_continue_and_fetch'].call_args.args[1], continuation)

    def test_authorize_already_completed_reads_session_without_replaying_callback(self):
        for url in ('https://chatgpt.com/', self.callback):
            with self.subTest(url=url):
                self.mocks['fetch_session'].reset_mock()
                result = login._login_from_authorize(self.session, 'test@example.com', url, 1)
                self.assertEqual(result['accessToken'], 'fresh')
                self.mocks['fetch_session'].assert_called_once_with(self.session)
        self.send.assert_not_called()
        self.mocks['_follow_continue_and_fetch'].assert_not_called()

    def test_repeated_otp_page_is_bounded(self):
        self.mocks['_validate_with_retry'].return_value = {'continue_url': 'https://auth.openai.com/email-verification'}
        with self.assertRaisesRegex(RuntimeError, '步骤重复'):
            login._login_via_email_otp(self.session, 'test@example.com', 1)
        self.assertLessEqual(self.mocks['_validate_with_retry'].call_count, 4)
        self.mocks['_follow_continue_and_fetch'].assert_not_called()

    def test_mfa_without_continuation_is_not_treated_as_success(self):
        self.mocks['_mfa_verify'].return_value = {}
        with self.assertRaisesRegex(RuntimeError, '登录尚未完成'):
            login._finish_login_result(self.session, 'test@example.com', self.mfa, 1)
        self.mocks['_follow_continue_and_fetch'].assert_not_called()
