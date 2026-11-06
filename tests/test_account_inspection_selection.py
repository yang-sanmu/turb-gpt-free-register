import unittest
from unittest.mock import patch

import test_account_inspection as fixtures
from core import db, account_inspection_service as service
from webui.app import create_app


class InspectionSelectionTests(unittest.TestCase):
    setUp = fixtures.AccountInspectionTests.setUp

    def update(self, account_id, **values):
        rows = db._load_accounts()
        next(r for r in rows if r['id'] == account_id).update(values)
        db._save_accounts(rows)

    def preview(self, mode='live', **options):
        return service.preview_inspection(mode=mode, **options)['total_count']

    def source(self, failures):
        run, _ = db.create_account_inspection(mode='live')
        for account_id, status in failures:
            self.assertTrue(db.save_account_inspection(run, failure={
                'account_id': account_id, 'email': db.get_account(account_id)['email'],
                'status': status, 'error': 'fixture', 'failed_at': '2026-11-01T10:00:00'}))
        run['status'] = 'completed'
        db.save_account_inspection(run)
        return run['id']

    def test_all_excludes_busy_and_deactivated(self):
        self.update(1, live_check_status='running')
        self.update(2, plan_check_status='queued')
        self.update(3, live_check_status='deactivated')
        self.assertEqual(self.preview(), 1)
        self.assertEqual(self.preview(archived='all'), 2)

    def test_selected_is_deduplicated_and_scoped(self):
        self.assertEqual(self.preview(scope='selected', account_ids=[1, 1, 5, 999]), 1)
        self.assertEqual(self.preview(scope='selected', account_ids=[1, 1, 5, 999], archived='all'), 2)

    def test_filtered_matches_list_across_pages(self):
        self.update(1, current_plan_type='plus', totp_secret='fixture', codex_status='success', created_at='2026-11-01T12:00:00')
        self.update(2, current_plan_type='plus', totp_secret='fixture', codex_status='success', created_at='2026-11-02T12:00:00')
        filters = {'plan': 'plus', 'totp_status': 'enabled', 'codex_status': 'success',
                   'q': 'example.test', 'date_from': '2026-11-01', 'date_to': '2026-11-02'}
        self.assertEqual(self.preview(scope='filtered', filters=filters), 2)
        page = db.list_accounts_page(limit=1, q=filters['q'], plan_filter='plus', totp_filter='enabled', codex_filter='success',
                                     date_from=filters['date_from'], date_to=filters['date_to'])
        self.assertEqual(page['total'], 2)
        self.assertEqual(len(page['items']), 1)

    def test_latest_failures_are_stage_specific(self):
        self.update(1, live_check_status='failed', plan_check_status='success')
        self.update(2, live_check_status='live', plan_check_status='failed')
        self.update(3, live_check_status='failed', plan_check_status='failed')
        self.update(4, live_check_status='deactivated', plan_check_status='failed')
        self.assertEqual(self.preview(scope='failed'), 2)
        self.assertEqual(self.preview(mode='plan', scope='failed'), 2)

    def test_missing_token_failure_is_available_to_latest_failed_retry(self):
        from core import plan_check_service
        self.update(3, current_plan_type='plus')
        run, _ = db.create_account_inspection([3], mode='plan')
        with patch.object(plan_check_service, '_wait_for_rate_slot'), \
             patch('core.chatgpt_plan.BrowserSession', side_effect=AssertionError('空 AT 不应发起网络请求')) as session, \
             patch.object(service, '_check_live') as live:
            service._run(run)
        self.assertEqual(run['plan_failed_count'], 1)
        self.assertEqual(db.get_account(3)['plan_check_status'], 'failed')
        self.assertEqual(db.get_account(3)['current_plan_type'], 'plus')
        self.assertEqual(self.preview(mode='plan', scope='failed'), 1)
        self.assertEqual(self.preview(mode='plan', scope='history_failed', source_inspection_id=run['id']), 1)
        session.assert_not_called()
        live.assert_not_called()

    def test_history_retries_only_requested_stage_and_skips_later_success(self):
        source = self.source([(1, 'failed'), (2, 'failed'), (3, 'plan_failed'), (4, 'deactivated')])
        self.update(1, live_check_status='live', live_checked_at='2026-11-02T10:00:00')
        self.update(2, live_check_status='live', live_checked_at='2026-10-30T10:00:00')
        options = dict(scope='history_failed', source_inspection_id=source)
        self.assertEqual(self.preview(**options), 1)  # Older success doesn't erase the later failure.
        self.assertEqual(self.preview(mode='plan', **options), 1)
        self.update(3, plan_check_status='success', plan_checked_at='2026-11-02T10:00:00')
        self.assertEqual(self.preview(mode='plan', **options), 0)

    def test_history_skips_success_completed_after_failure_even_if_started_before_it(self):
        source = self.source([(1, 'failed'), (2, 'plan_failed')])
        options = dict(scope='history_failed', source_inspection_id=source)
        live_run, _ = db.create_account_inspection(mode='live', selection=options)
        with patch.object(db, '_now', return_value='2026-11-01T10:01:00'):
            db.update_account_liveness(1, {'ok': True, 'checked_at': '2026-11-01T09:59:00'})
            db.update_account_plan_check(2, result={'ok': True, 'checked_at': '2026-11-01T09:59:00'})
        self.assertEqual(self.preview(**options), 0)
        self.assertEqual(self.preview(mode='plan', **options), 0)
        with patch.object(service, '_check_live') as live:
            service._run(live_run)
        live.assert_not_called()
        self.assertEqual(live_run['skipped_count'], 1)

    def test_history_excludes_deleted_and_email_changed_accounts(self):
        source = self.source([(1, 'failed'), (2, 'failed')])
        db.delete_account(1)
        self.update(2, email='changed@example.test')
        self.assertEqual(self.preview(scope='history_failed', source_inspection_id=source), 0)

    def test_retry_rechecks_later_success_before_network_call(self):
        source = self.source([(1, 'failed'), (2, 'failed')])
        selection = {'scope': 'history_failed', 'source_inspection_id': source}
        run, _ = db.create_account_inspection(mode='live', selection=selection)
        self.update(1, live_check_status='live', live_checked_at='2026-11-02T10:00:00')
        with patch.object(service, '_check_live', return_value={'ok': True, 'plan_result': {'ok': True}}) as live:
            service._run(run)
        self.assertEqual([c.args[0]['id'] for c in live.call_args_list], [2])
        self.assertEqual(run['skipped_count'], 1)
        self.assertEqual(run['processed_count'], 2)

    def test_plan_retry_never_logs_in(self):
        source = self.source([(1, 'plan_failed'), (2, 'failed')])
        run, _ = db.create_account_inspection(mode='plan', selection={'scope': 'history_failed', 'source_inspection_id': source})
        with patch.object(service, '_check_live') as live, patch.object(service, '_check_plan', return_value={'ok': True}) as plan:
            service._run(run)
        live.assert_not_called()
        self.assertEqual([c.args[0]['id'] for c in plan.call_args_list], [1])

    def test_latest_retry_rechecks_status_and_busy_after_snapshot(self):
        self.update(1, live_check_status='failed')
        self.update(2, live_check_status='failed')
        run, _ = db.create_account_inspection(mode='live', selection={'scope': 'failed'})
        self.update(1, live_check_status='live')
        self.update(2, plan_check_status='running')
        with patch.object(service, '_check_live') as live:
            service._run(run)
        live.assert_not_called()
        self.assertEqual(run['skipped_count'], 2)

    def test_empty_selection_does_not_create_task(self):
        with self.assertRaisesRegex(ValueError, '没有可执行'):
            service.start_inspection(mode='live', scope='failed')
        self.assertIsNone(db.list_account_inspections()['active'])

    def test_source_validation_and_legacy_history(self):
        options = dict(scope='history_failed', source_inspection_id=999)
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.preview(**options)
        run, _ = db.create_account_inspection()
        options['source_inspection_id'] = run['id']
        with self.assertRaisesRegex(ValueError, '尚未结束'):
            self.preview(**options)
        run.update(status='completed', failure_details_available=False)
        db.save_account_inspection(run)
        with self.assertRaisesRegex(ValueError, '未保存失败明细'):
            self.preview(**options)

    def test_preview_and_start_api_validate_without_broadening_scope(self):
        app = create_app(auth_code='inspection-test')
        client = app.test_client()
        headers = {'X-Auth-Code': 'inspection-test'}
        self.assertEqual(client.post('/api/accounts/inspections/preview', json={'mode': 'live'}).status_code, 401)
        invalid = [
            {'scope': 'bad'}, {'scope': ['all']}, {'scope': 'selected', 'account_ids': []},
            {'scope': 'selected', 'account_ids': [True]}, {'scope': 'selected', 'account_ids': [2**63]},
            {'scope': 'all', 'account_ids': [1]}, {'scope': 'filtered', 'filters': {'unknown': 'x'}},
            {'scope': 'filtered', 'filters': {'q': []}}, {'scope': 'history_failed', 'source_inspection_id': True},
            {'scope': 'all', 'source_inspection_id': 1}, {'scope': 'all', 'filters': {}}, {'typo': 1},
        ]
        with patch.object(service._EXECUTOR, 'submit') as submit:
            for body in invalid:
                for route in ('/api/accounts/inspections', '/api/accounts/inspections/preview'):
                    with self.subTest(body=body, route=route):
                        response = client.post(route, json={'mode': 'live', **body}, headers=headers)
                        self.assertEqual(response.status_code, 400, response.get_json())
            submit.assert_not_called()
            body = {'mode': 'live', 'scope': 'selected', 'account_ids': [1, 3]}
            response = client.post('/api/accounts/inspections/preview', json=body, headers=headers)
            self.assertEqual(response.get_json()['total_count'], 2)
            self.assertIsNone(db.list_account_inspections()['active'])
            response = client.post('/api/accounts/inspections', json=body, headers=headers)
            self.assertEqual(response.status_code, 202)
            run = submit.call_args.args[1]
            self.assertEqual(run['account_ids'], [3, 1])
            self.assertEqual(run['selection'], {'scope': 'selected'})
            self.assertNotIn('account_ids', response.get_json()['task'])
            self.assertEqual(client.post('/api/accounts/inspections', json=body, headers=headers).status_code, 409)
