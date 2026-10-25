"""Integration regressions use fake external APIs and disposable storage."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch, Mock

import integrations as clients


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='shiying-integrations-')
        overrides = dict(DATA_DIR=cls.temp.name, SECRET_KEY=secrets.token_hex(32),
                         ADMIN_USERNAME='tester', ADMIN_PASSWORD='Temporary-Test-12345', ENABLE_SCHEDULER='false')
        with patch.dict(os.environ, overrides):
            spec = importlib.util.spec_from_file_location('integration_test_app', Path(__file__).parents[1] / 'app.py')
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)
        cls.app = cls.mod.app
        cls.app.config['TESTING'] = True

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        with self.mod.database() as db:
            db.execute('DELETE FROM items')
            db.execute('DELETE FROM attempts')
            db.execute("DELETE FROM settings WHERE key='integration_config'")
        self.client = self.app.test_client()
        response = self.client.post('/api/login', json={'username': 'tester', 'password': 'Temporary-Test-12345'})
        self.headers = {'X-CSRF-Token': response.json['csrf']}
        # External share reads and background threads are isolated in disposable API tests.
        for name, options in [('start_source_refresh', {'return_value': True}),
                              ('read_share_episodes', {'side_effect': clients.IntegrationError('测试目录暂不可读')})]:
            mocker = patch.object(self.mod, name, **options)
            mocker.start()
            self.addCleanup(mocker.stop)

    def metadata(self, **changes):
        today = dt.datetime.now(self.mod.TZ).date()
        value = dict(id=123, media_type='tv', poster_path='', overview='测试简介', original_title='Test',
                     title='测试作品', year=2026, status='Returning Series', total_episodes=4,
                     air_dates={'1': (today - dt.timedelta(days=2)).isoformat(),
                                '2': (today - dt.timedelta(days=1)).isoformat(), '3': (today + dt.timedelta(days=2)).isoformat(), '4': ''})
        value.update(changes)
        return value

    def record(self, **changes):
        value = dict(title='测试作品', kind='tv', season=1, total_episodes=4,
                     library_episodes='1', tmdb=self.metadata(), next_check='2020-01-01')
        value.update(changes)
        response = self.client.post('/api/items', json=value, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['item']

    def configure(self):
        response = self.client.put('/api/integrations', json=dict(tmdb_credential='TMDB-ONLY-TEST',
                   emby_url='https://emby.example.com/emby', emby_api_key='EMBY-ONLY-TEST', auto_sync=True,
                   interval_minutes=30), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return response

    def perform_sync(self, comparison=None, error=None, metadata_error=None, side_effect=None):
        self.configure()
        tmdb = Mock()
        tmdb.tracking_metadata.return_value = self.metadata()
        tmdb.tracking_metadata.side_effect = metadata_error
        emby = Mock()
        emby.compare.return_value = comparison or dict(state='matched', episodes=[1, 2], item_ids=['series'], error='', unnumbered=0)
        emby.compare.side_effect = side_effect or error
        self.mod.SYNC_LOCK.acquire()
        with patch.object(self.mod, 'tmdb_client', return_value=tmdb), patch.object(self.mod, 'emby_client', return_value=emby):
            self.mod.run_sync()
        return self.client.get('/api/items').json['items'][0]

    def test_tmdb_metadata_is_read_only_but_collection_fields_can_be_saved(self):
        original = self.record()
        for field, value in dict(title='手动改名', kind='anime', year=2020, season=2,
                                 total_episodes=10, finished=True).items():
            with self.subTest(field=field):
                response = self.client.put('/api/items/' + original['id'],
                    json={**original, field: value}, headers=self.headers)
                self.assertEqual(response.status_code, 400, response.json)
                self.assertIn('TMDB', response.json['error'])
        response = self.client.put('/api/items/' + original['id'],
            json={**original, 'notes': '保留此发布者', 'tracking': False}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        for field in ('title', 'kind', 'year', 'season', 'total_episodes', 'finished'):
            self.assertEqual(response.json['item'][field], original[field])
        self.assertEqual(response.json['item']['notes'], '保留此发布者')
        self.assertFalse(response.json['item']['tracking'])

    def test_secret_storage_and_responses(self):
        response = self.configure()
        self.assertTrue(response.json['tmdb_configured'])
        stored = self.mod.setting('integration_config')
        for secret in ('TMDB-ONLY-TEST', 'EMBY-ONLY-TEST'):
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, self.client.get('/api/backup').text)
        cfg = self.mod.integration_config()
        self.assertEqual(self.mod.decrypt_secret(cfg['tmdb_credential']), 'TMDB-ONLY-TEST')
        self.client.put('/api/integrations', json={'tmdb_credential': '', 'emby_api_key': ''}, headers=self.headers)
        self.assertTrue(self.client.get('/api/integrations').json['emby_configured'])

    def test_integrations_require_login_and_csrf(self):
        anon = self.app.test_client()
        for path in ('/api/integrations', '/api/tmdb/search?q=test', '/api/tmdb/poster?path=/a.jpg'):
            self.assertEqual(anon.get(path).status_code, 401)
        for path in ('/api/sync', '/api/tmdb/track', '/api/integrations/test'):
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.assertEqual(self.client.put('/api/integrations', json={}).status_code, 403)

    def test_aired_and_future_dates(self):
        item = self.record()
        self.assertEqual(item['aired_episodes'], '1-2')
        self.assertEqual(item['missing_episodes'], '2')
        self.assertEqual(item['not_aired_count'], 2)
        item = self.record(library_episodes='', tmdb=self.metadata(air_dates={'1': '2099-01-01', '2': ''}))
        self.assertEqual(item['status'], 'upcoming')
        self.assertFalse(item['missing_episodes'])

    def test_today_schedule_does_not_create_missing_episodes(self):
        today = dt.datetime.now(self.mod.TZ).date()
        dates = {'1': (today - dt.timedelta(days=1)).isoformat(), '2': today.isoformat(),
                 '3': (today + dt.timedelta(days=1)).isoformat(), '4': ''}
        saved = self.record(sources=[dict(id='selected',share_url='https://115.com/s/test')],active_source_id='selected',tmdb=self.metadata(air_dates=dates))
        item = self.mod.decorate(dict(saved, emby={'state': 'matched'}))
        self.assertEqual(item['aired_episodes'], '1')
        self.assertEqual(item['airing_today_episodes'], '2')
        self.assertEqual(item['airing_today_count'], 1)
        self.assertEqual(item['not_aired_count'], 2)
        self.assertEqual(item['missing_episodes'], '')
        self.assertFalse(item['needs_attention'])
        # Actual source availability and actual library contents remain independent of the schedule.
        sourced = self.mod.decorate(dict(item, sources=[dict(id='s', episodes='1-2', broken=False)], active_source_id='s'))
        self.assertEqual(sourced['missing_episodes'], '2')
        self.assertTrue(sourced['needs_attention'])
        collected = self.mod.decorate(dict(sourced, library_episodes='1-2'))
        self.assertEqual(collected['aired_count'], 1)
        self.assertEqual(collected['library_count'], 2)
        self.assertFalse(collected['needs_attention'])

    def test_shanghai_midnight_moves_schedule_without_early_missing(self):
        saved = self.record(library_episodes='1', tmdb=self.metadata(
            air_dates={'1': '2026-10-22', '2': '2026-10-23', '3': '2026-10-24', '4': ''}))
        saved['emby'] = {'state': 'matched'}
        real_datetime = dt.datetime
        class FrozenDateTime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
        for instant, aired, today, missing in [
            (real_datetime(2026, 10, 22, 15, 59, tzinfo=dt.timezone.utc), '', '1', ''),
            (real_datetime(2026, 10, 22, 16, 0, tzinfo=dt.timezone.utc), '1', '2', ''),
            (real_datetime(2026, 10, 23, 16, 0, tzinfo=dt.timezone.utc), '1-2', '3', '2')]:
            with self.subTest(instant=instant), patch.object(self.mod.dt, 'datetime', FrozenDateTime):
                item = self.mod.decorate(saved)
                self.assertEqual(item['aired_episodes'], aired)
                self.assertEqual(item['airing_today_episodes'], today)
                self.assertEqual(item['missing_episodes'], missing)

    def test_today_movie_release_stays_separate_from_library(self):
        today = dt.datetime.now(self.mod.TZ).date().isoformat()
        saved = self.record(kind='movie', total_episodes=1, library_episodes='',
            tmdb=self.metadata(media_type='movie', total_episodes=1, air_dates={'1': today}))
        item = self.mod.decorate(dict(saved, emby={'state': 'matched'}))
        self.assertEqual(item['aired_count'], 0)
        self.assertEqual(item['airing_today_count'], 1)
        self.assertEqual(item['not_aired_count'], 0)
        self.assertEqual(item['status'], 'premiering')
        self.assertFalse(item['needs_attention'])
        item = self.mod.decorate(dict(item, library_episodes='1'))
        self.assertEqual(item['library_count'], 1)
        self.assertEqual(item['status'], 'complete')

    def test_today_premiere_is_not_upcoming_or_early_missing(self):
        today = dt.datetime.now(self.mod.TZ).date().isoformat()
        saved = self.record(library_episodes='', tmdb=self.metadata(
            air_dates={'1': today, '2': today, '3': '2099-01-01', '4': ''}))
        item = self.mod.decorate(dict(saved, emby={'state': 'absent'}))
        self.assertEqual(item['status'], 'premiering')
        self.assertEqual(item['aired_count'], 0)
        self.assertEqual(item['airing_today_count'], 2)
        self.assertFalse(item['missing_episodes'])
        self.assertFalse(item['needs_attention'])

    def test_unbound_aired_work_is_wanted_then_bound_work_is_pending(self):
        saved = self.record(library_episodes='')
        item = self.mod.decorate(dict(saved, emby={'state': 'absent'}))
        self.assertEqual(item['status'], 'wanted')
        self.assertEqual(item['library_count'], 0)
        self.assertEqual(item['missing_episodes'], '1-2')
        self.assertEqual([reason['code'] for reason in item['attention_reasons']], ['source_missing'])
        bound = self.mod.decorate(dict(item,sources=[dict(id='selected',episodes='',broken=False)],active_source_id='selected'))
        self.assertEqual(bound['status'], 'pending')
        self.assertEqual([reason['code'] for reason in bound['attention_reasons']], ['missing'])
        self.assertEqual(self.mod.decorate(dict(item, library_episodes='1-2'))['status'], 'wanted')
        caught_up = self.mod.decorate(dict(bound, library_episodes='1-2'))
        self.assertEqual(caught_up['status'], 'updating')
        self.assertEqual(caught_up['attention_reasons'], [])
        self.assertEqual(self.mod.decorate(dict(item, library_episodes='1-4'))['status'], 'complete')
        self.assertFalse(self.mod.decorate(dict(item, tracking=False))['needs_attention'])

    def test_attention_dates_and_unaired_episodes_do_not_create_work(self):
        item = self.mod.decorate(dict(self.record(sources=[dict(id='selected',share_url='https://115.com/s/test')],active_source_id='selected',library_episodes='1-2'), emby={'state': 'matched'}))
        self.assertTrue(item['due'])  # Old manual dates remain available for compatibility.
        self.assertEqual(item['not_aired_count'], 2)
        self.assertEqual(item['attention_reasons'], [])
        self.assertFalse(item['needs_attention'])
        upcoming = self.record(library_episodes='', tmdb=self.metadata(air_dates={'1': '2099-01-01'}))
        self.assertTrue(upcoming['due'])
        self.assertFalse(upcoming['needs_attention'])

    def test_attention_missing_requires_verified_emby(self):
        saved = self.record(sources=[dict(id='selected',share_url='https://115.com/s/test')],active_source_id='selected',)
        for state in ('never', 'unmatched', 'error', 'matched'):
            with self.subTest(state=state):
                item = self.mod.decorate(dict(saved, emby={'state': state}))
                codes = [reason['code'] for reason in item['attention_reasons']]
                self.assertEqual('missing' in codes, state == 'matched')
                self.assertEqual('sync_error' in codes, state == 'error')
                self.assertEqual(item['needs_attention'], state in ('error', 'matched'))
                if state == 'matched':
                    self.assertEqual(item['attention_reasons'][0],
                                     dict(code='missing', label='待补集', detail='等待入库：2'))

    def test_attention_manual_progress_and_current_source_only(self):
        saved = self.record(tmdb=None, sources=[
            dict(id='current', sharer='当前来源', episodes='1-2'),
            dict(id='history', sharer='历史来源', episodes='1-4', broken=True)], active_source_id='current')
        self.assertEqual([reason['code'] for reason in saved['attention_reasons']], ['missing'])
        current_broken = copy.deepcopy(saved)
        current_broken['sources'][0]['broken'] = True
        item = self.mod.decorate(current_broken)
        self.assertEqual([reason['code'] for reason in item['attention_reasons']], ['missing', 'source_error'])
        complete = self.mod.decorate(dict(current_broken, library_episodes='1-4'))
        self.assertEqual(complete['status'], 'complete')
        self.assertEqual(complete['attention_reasons'], [])
        self.assertFalse(complete['needs_attention'])

    def test_attention_aggregates_sync_failures_even_after_collection(self):
        saved = self.record(library_episodes='1-4', tmdb=self.metadata(metadata_error='资料接口暂时不可用'))
        item = self.mod.decorate(dict(saved, emby={'state': 'error', 'error': '片库连接失败'},
                                      re0={'error': '固定来源查询失败'}))
        self.assertEqual(item['status'], 'complete')
        self.assertEqual(item['attention_reasons'], [dict(code='sync_error', label='同步失败',
            detail='TMDB：资料接口暂时不可用；Emby：片库连接失败；RE0：固定来源查询失败')])
        self.assertTrue(item['needs_attention'])
        saved['tmdb']['metadata_error'] = ''
        recovered = self.mod.decorate(dict(saved, emby={'state': 'matched', 'error': '过期错误'},
                                           re0={'error': ''}))
        self.assertFalse(recovered['needs_attention'])
        self.assertEqual(recovered['attention_reasons'], [])

    def test_attention_paused_keeps_reasons_without_queueing(self):
        saved = self.record(tmdb=self.metadata(metadata_error='资料更新失败'),
            sources=[dict(id='current', episodes='1-2', broken=True)], active_source_id='current')
        item = self.mod.decorate(dict(saved, tracking=False, emby={'state': 'matched'}))
        self.assertEqual([reason['code'] for reason in item['attention_reasons']],
                         ['missing', 'source_error', 'sync_error'])
        self.assertFalse(item['needs_attention'])
        self.assertTrue(self.mod.decorate(dict(item, tracking=True))['needs_attention'])

    def test_attention_sync_recovery_clears_the_current_failure(self):
        self.record(sources=[dict(id='selected',share_url='https://115.com/s/test')],active_source_id='selected',library_episodes='1-2')
        failed = self.perform_sync(error=clients.IntegrationError('模拟连接失败'))
        self.assertEqual([reason['code'] for reason in failed['attention_reasons']], ['sync_error'])
        self.assertTrue(failed['needs_attention'])
        recovered = self.perform_sync()
        self.assertEqual(recovered['attention_reasons'], [])
        self.assertFalse(recovered['needs_attention'])

    def test_track_has_no_default_check_date_but_keeps_admin_explicit_date(self):
        fake = Mock()
        fake.tracking_metadata.return_value = self.metadata()
        with patch.object(self.mod, 'tmdb_client', return_value=fake):
            body = dict(tmdb_id=123, media_type='tv', season=1)
            first = self.client.post('/api/tmdb/track', json=body, headers=self.headers)
            self.assertEqual(first.status_code, 201, first.json)
            self.assertEqual(first.json['item']['next_check'], '')
            self.assertFalse(first.json['item']['due'])
            explicit = self.client.post('/api/tmdb/track',
                json={**body, 'season': 2, 'next_check': '2030-05-01'}, headers=self.headers)
            self.assertEqual(explicit.status_code, 201, explicit.json)
            self.assertEqual(explicit.json['item']['next_check'], '2030-05-01')

    def test_track_deduplicates_by_type_and_season(self):
        fake = Mock()
        fake.tracking_metadata.return_value = self.metadata()
        with patch.object(self.mod, 'tmdb_client', return_value=fake):
            body = dict(tmdb_id=123, media_type='tv', season=1)
            first = self.client.post('/api/tmdb/track', json=body, headers=self.headers)
            repeat = self.client.post('/api/tmdb/track', json=body, headers=self.headers)
            self.assertEqual(first.status_code, 201, first.json)
            self.assertTrue(repeat.json['already_exists'])
            self.assertEqual(first.json['item']['id'], repeat.json['item']['id'])
            other = self.client.post('/api/tmdb/track', json={**body, 'season': 2}, headers=self.headers)
            self.assertNotEqual(other.json['item']['id'], first.json['item']['id'])
            self.assertEqual(first.json['item']['events'][0]['action'], 'tracked')

    def test_tmdb_search_uses_correct_auth_and_filters_people(self):
        raw = {'page': 1, 'total_pages': 1, 'results': [
            {'id': 10, 'media_type': 'person', 'name': 'Actor'},
            {'id': 11, 'media_type': 'tv', 'name': '剧集', 'first_air_date': '2024-01-01'},
            {'id': 12, 'media_type': 'movie', 'title': '电影', 'release_date': '2025-01-01'}]}
        with patch('integrations.fetch', return_value=raw) as fetch:
            result = clients.TMDBClient('a' * 32).search('测试')
            self.assertEqual(len(result['results']), 2)
            self.assertIn('api_key=', fetch.call_args.args[0])
            self.assertNotIn('Authorization', fetch.call_args.args[1])
            clients.TMDBClient('read-access-token').search('测试')
            self.assertEqual(fetch.call_args.args[1]['Authorization'], 'Bearer read-access-token')

    def test_emby_filters_virtual_expands_multi_and_merges(self):
        def remote(url, headers=None, **kwargs):
            self.assertEqual(headers['X-Emby-Token'], 'test-key')
            if '/Items?' in url:
                return {'TotalRecordCount': 4, 'Items': [
                    {'Id': 'a', 'Type': 'Series', 'ProviderIds': {'Tmdb': '123'}},
                    {'Id': 'b', 'Type': 'Series', 'ProviderIds': {'tmdb': '123'}},
                    {'Id': 'wrong', 'Type': 'Series', 'ProviderIds': {'Tmdb': '999'}},
                    {'Id': 'movie', 'Type': 'Movie', 'ProviderIds': {'Tmdb': '123'}}]}
            if '/Shows/a/' in url:
                return {'Items': [dict(IndexNumber=1, IndexNumberEnd=2, ParentIndexNumber=1),
                                  dict(IndexNumber=3, LocationType='Virtual'),
                                  dict(IndexNumber=4, IsPlaceHolder=True),
                                  dict(IndexNumber=5, ParentIndexNumber=2), dict(Name='Unnumbered')]}
            if '/Shows/b/' in url:
                return {'Items': [dict(IndexNumber=2), dict(IndexNumber=4)]}
            self.fail('wrong series queried: ' + url)
        with patch('integrations.fetch', side_effect=remote):
            result = clients.EmbyClient('https://example.com/emby', 'test-key').compare('tv', 123, 1)
        self.assertEqual(result['episodes'], [1, 2, 4])
        self.assertEqual(result['item_ids'], ['a', 'b'])
        self.assertEqual(result['unnumbered'], 1)

    def test_emby_pagination(self):
        client = clients.EmbyClient('https://example.com', 'test-key')
        client.get = Mock(side_effect=[{'Items': [{'Id': str(i)} for i in range(200)], 'TotalRecordCount': 201},
                                       {'Items': [{'Id': '200'}], 'TotalRecordCount': 201}])
        self.assertEqual(len(client.pages('/Items')), 201)
        self.assertEqual(client.get.call_args.kwargs['StartIndex'], 200)
        client.get = Mock(return_value={'Items': [], 'TotalRecordCount': 10})
        with self.assertRaises(clients.IntegrationError):
            client.pages('/Items')

    def test_emby_absent_requires_successful_id_and_title_searches(self):
        client = clients.EmbyClient('http://example.com:8096', 'test-key')
        client.pages = Mock(return_value=[])
        result = client.compare('tv', 123, 1, ('测试作品', 'Test Story'))
        self.assertEqual(result['state'], 'absent')
        self.assertEqual(result['episodes'], [])
        self.assertEqual(result['error'], '')
        self.assertEqual(client.pages.call_count, 3)
        self.assertEqual(client.pages.call_args.kwargs['SearchTerm'], 'Test Story')
        client.pages = Mock(side_effect=[[], clients.IntegrationError('连接失败')])
        with self.assertRaises(clients.IntegrationError):
            client.compare('tv', 123, 1, ('测试作品',))

    def test_emby_same_title_with_different_metadata_still_needs_review(self):
        client = clients.EmbyClient('http://example.com:8096', 'test-key')
        client.pages = Mock(side_effect=[[], [dict(Id='candidate', Name='测试作品', Type='Series', ProviderIds={})]])
        result = client.compare('tv', 123, 1, ('测试作品',))
        self.assertEqual(result['state'], 'unmatched')
        self.assertIn('刮削', result['error'])
        client.pages = Mock(side_effect=[[], [dict(Id='other', Name='另一部作品', Type='Series')]])
        self.assertEqual(client.compare('tv', 123, 1, ('测试作品',))['state'], 'absent')

    def test_sync_replaces_only_success_and_keeps_sources(self):
        source = dict(id='a', sharer='固定分享者', episodes='1-2', version='4K')
        self.record(sources=[source], active_source_id='a', notes='请保留')
        item = self.perform_sync()
        self.assertEqual(item['library_episodes'], '1-2')
        self.assertEqual(item['sources'][0]['sharer'], '固定分享者')
        self.assertEqual(item['notes'], '请保留')
        self.assertEqual(item['emby']['state'], 'matched')
        self.assertTrue(any(e['action'] == 'emby_sync' for e in item['events']))

    def test_sync_failure_or_unmatched_preserves_library(self):
        self.record(library_episodes='1-3')
        item = self.perform_sync(error=clients.IntegrationError('模拟连接失败'))
        self.assertEqual(item['library_episodes'], '1-3')
        self.assertEqual(item['emby']['state'], 'error')
        item = self.perform_sync(comparison=dict(state='unmatched', episodes=[], item_ids=[], error='待核对', unnumbered=0))
        self.assertEqual(item['library_episodes'], '1-3')
        self.assertEqual(item['emby']['state'], 'unmatched')

    def test_absent_result_preserves_conflicting_historical_progress(self):
        self.record(library_episodes='1-3')
        item = self.perform_sync(comparison=dict(state='absent', episodes=[], item_ids=[], error='', unnumbered=0))
        self.assertEqual(item['library_episodes'], '1-3')
        self.assertEqual(item['emby']['state'], 'unmatched')
        self.assertIn('历史入库', item['emby']['error'])

    def test_absent_result_saves_zero_library_without_metadata_warning(self):
        self.record(library_episodes='')
        item = self.perform_sync(comparison=dict(state='absent', episodes=[], item_ids=[], error='', unnumbered=0))
        self.assertEqual(item['emby']['state'], 'absent')
        self.assertEqual(item['library_count'], 0)
        self.assertEqual(item['status'], 'wanted')
        self.assertEqual([reason['code'] for reason in item['attention_reasons']], ['source_missing'])
        self.assertTrue(any(event['action'] == 'emby_absent' for event in item['events']))

    def test_sync_merges_concurrent_manual_source_edits(self):
        saved = self.record()
        def edit_during_network(*args):
            latest = self.client.get('/api/items').json['items'][0]
            latest['notes'] = '同步期间新写的备注'
            latest['sources'] = [dict(id='new', sharer='新来源', episodes='1')]
            latest['active_source_id'] = 'new'
            response = self.client.put('/api/items/' + saved['id'], json=latest, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            return dict(state='matched', episodes=[1, 2], item_ids=['series'], error='', unnumbered=0)
        result = self.perform_sync(side_effect=edit_during_network)
        self.assertEqual(result['notes'], '同步期间新写的备注')
        self.assertEqual(result['sources'][0]['sharer'], '新来源')
        self.assertEqual(result['library_episodes'], '1-2')

    def test_paused_records_are_not_synced(self):
        self.record(tracking=False)
        item = self.perform_sync()
        self.assertEqual(item['library_episodes'], '1')
        self.assertEqual(item['emby']['state'], 'never')

    def test_backup_restores_tmdb_and_events_with_extra_emby_episode(self):
        item = self.record(library_episodes='1-5')
        backup = self.client.get('/api/backup').json
        with self.mod.database() as db:
            db.execute('DELETE FROM items')
        response = self.client.post('/api/import', json=backup, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        restored = self.client.get('/api/items').json['items'][0]
        self.assertEqual(restored['tmdb']['id'], 123)
        self.assertEqual(restored['library_episodes'], '1-5')
        self.assertEqual(restored['events'], item['events'])
        self.assertEqual(restored['emby']['state'], 'never')

    def test_emby_url_does_not_accept_embedded_credentials(self):
        for url in ('ftp://public.example.com', 'https://u:pass@example.com', 'https://example.com?api_key=secret'):
            with self.assertRaises(clients.IntegrationError):
                clients.valid_emby_url(url)
        self.assertEqual(clients.valid_emby_url('https://example.com/emby/'), 'https://example.com/emby')
        self.assertEqual(clients.valid_emby_url('http://public.example.com:8096/emby/'), 'http://public.example.com:8096/emby')

    def test_poster_path_blocks_arbitrary_remote_urls(self):
        self.assertEqual(clients.poster_path('/poster123.jpg'), '/poster123.jpg')
        for value in ('https://other.test/x.jpg', '/../private.jpg', '//other.test/a.jpg', '/a.svg'):
            self.assertEqual(clients.poster_path(value), '')


if __name__ == '__main__':
    unittest.main()
