import unittest
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import share_scanner as scanner
from integrations import IntegrationError
import test_integrations as integration_tests


class ShareScannerTests(unittest.TestCase):
    def setUp(self):
        scanner._CACHE.clear()

    def test_extracts_only_allowlisted_share_identifiers(self):
        self.assertEqual(scanner.share_identity('https://115cdn.com/s/abc123?password=abcd'), ('abc123', 'abcd'))
        self.assertEqual(scanner.share_identity('http://115.com/s/abc123', 'test'), ('abc123', 'test'))
        for url in ('https://115.com.evil.test/s/abc123', 'https://u:pw@115.com/s/abc123',
                    'file:///s/abc123', 'http://127.0.0.1/s/abc123', 'https://115.com:444/s/abc123',
                    'https://115.com/private', 'https://115.com/s/abc123/more'):
            with self.subTest(url=url), self.assertRaises(IntegrationError):
                scanner.share_identity(url)

    def test_episode_names_and_season_scope(self):
        for path, season, expected in [
            ('/Season 1/Show.2026.S01E06.2160p.HEVC.mkv', 1, {6}),
            ('/第四季/综艺.第十二期(上).mp4', 4, {12}),
            ('/S04/Show.4x05.mkv', 4, {5}), ('/S01/E01-E03.mkv', 1, {1, 2, 3}),
            ('/S01/Show.S01E01E02.mkv', 1, {1, 2}), ('/Season 1/07.mp4', 1, {7}),
            ('/Season 2/Show.S02E03.mkv', 1, None), ('/Show.E03.mkv', 2, None),
            ('/Season 1/Show.2160p.mp4', 1, None), ('/S01/E1080p.mp4', 1, None)]:
            with self.subTest(path=path):
                self.assertEqual(scanner.episode_numbers(path, season), expected)
        self.assertEqual(scanner.episode_numbers('/S04/show.20261024.mp4', 4, {'5': '2026-10-24'}), {5})
        self.assertIsNone(scanner.episode_numbers('/S04/show.20261024.mp4', 4, {'5': '2026-10-24', '6': '2026-10-24'}))
        self.assertTrue(scanner.skip_path('/Season 1/预告.S01E01.mkv', 1))
        self.assertTrue(scanner.skip_path('/Other {tmdb-999}/S01E01.mkv', 1, 123))

    def test_recurses_pages_and_deduplicates_versions(self):
        def directory(cid, name): return dict(cid=str(cid), n=name, fc=0)
        def video(fid, name): return dict(fid=str(fid), n=name, fc=1)
        pages = {
            ('0', '0'): (1, [directory(1, 'Show {tmdb-123}')]),
            ('1', '0'): (3, [directory(2, 'Season 1'), directory(3, 'Season 2'), directory(4, 'Trailers')]),
            ('2', '0'): (4, [video(10, 'Show.S01E01.2160p.mkv'), video(11, 'Show.S01E01.1080p.mp4')]),
            ('2', '2'): (4, [video(12, 'Show.S01E02.mkv'), video(13, 'Show.S01E02.srt')]),
        }
        def fetch(url, **kwargs):
            self.assertTrue(url.startswith('https://webapi.115.com/share/snap?'))
            params = parse_qs(urlsplit(url).query)
            count, rows = pages[(params['cid'][0], params['offset'][0])]
            return dict(state=True, data=dict(count=count, list=rows))
        with patch.object(scanner, 'fetch', side_effect=fetch) as request:
            result = scanner.read_share_episodes('https://115.com/s/abc123', season=1, tmdb_id=123, minimum_interval=0)
            self.assertEqual(result['state'], 'complete')
            self.assertEqual(result['episodes'], '1-2')
            self.assertEqual(result['episode_count'], 2)
            self.assertEqual(result['video_count'], 3)
            self.assertEqual(request.call_count, 4)
            scanner.read_share_episodes('https://115.com/s/abc123', season=1, tmdb_id=123, minimum_interval=0)
            self.assertEqual(request.call_count, 4)

    def test_uncertain_and_partial_reads_are_never_reported_as_complete(self):
        rows = [dict(fid='1', n='Show.S01E01.mkv', fc=1), dict(fid='2', n='正片.2160p.mkv', fc=1)]
        with patch.object(scanner, 'fetch', return_value=dict(state=True, data=dict(count=2, list=rows))):
            result = scanner.read_share_episodes('https://115.com/s/partial123', minimum_interval=0)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['unrecognized_count'], 1)
        self.assertEqual(result['episodes'], '1')
        for response in (dict(state=False, errno=123), dict(state=True, data=dict(count=5, list=[])),
                         dict(state=True, data=dict(count=1.2, list=rows))):
            scanner._CACHE.clear()
            with patch.object(scanner, 'fetch', return_value=response), self.assertRaises(IntegrationError):
                scanner.read_share_episodes('https://115.com/s/error123', minimum_interval=0)


class SourceScanApiTests(unittest.TestCase):
    setUpClass = classmethod(integration_tests.IntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration_tests.IntegrationTests.tearDownClass.__func__)
    setUp = integration_tests.IntegrationTests.setUp
    record = integration_tests.IntegrationTests.record
    metadata = integration_tests.IntegrationTests.metadata
    configure = integration_tests.IntegrationTests.configure
    perform_sync = integration_tests.IntegrationTests.perform_sync

    def saved(self, item):
        with self.mod.database() as db:
            return self.mod.get_item(db, item['id'])

    def result(self, **changes):
        return dict(dict(state='complete', episodes='1-6', episode_count=6,
                         checked_at='2026-10-25T04:00:00+08:00'), **changes)

    def test_background_updates_only_source_and_preserves_scan_when_saving_notes(self):
        item = self.record(sources=[dict(id='s', share_url='https://115.com/s/abc123', episodes='1-2')])
        with patch.object(self.mod, 'read_share_episodes', return_value=self.result()):
            self.mod.refresh_source_episodes(item['id'])
        saved = self.saved(item)
        self.assertEqual(saved['sources'][0]['episodes'], '1-6')
        self.assertEqual(saved['library_episodes'], item['library_episodes'])
        self.assertEqual(saved['sources'][0]['episode_scan']['state'], 'complete')
        response = self.client.put('/api/items/' + item['id'], json=dict(saved, notes='新备注'), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['item']['sources'][0]['episode_scan'], saved['sources'][0]['episode_scan'])

    def test_uncertain_background_reads_keep_previous_count(self):
        item = self.record(sources=[dict(id='s', share_url='https://115.com/s/abc123', episodes='1-2')])
        for value in (self.result(state='partial', unrecognized_count=1), self.result(state='empty', episodes='', episode_count=0)):
            with patch.object(self.mod, 'read_share_episodes', return_value=value):
                self.mod.refresh_source_episodes(item['id'])
            self.assertEqual(self.saved(item)['sources'][0]['episodes'], '1-2')
        with patch.object(self.mod, 'read_share_episodes', side_effect=IntegrationError('无法读取目录')):
            self.mod.refresh_source_episodes(item['id'])
        self.assertEqual(self.saved(item)['sources'][0]['episodes'], '1-2')
        self.assertEqual(self.saved(item)['sources'][0]['episode_scan']['state'], 'error')

    def test_late_scan_cannot_override_changed_source_manual_input_pause_or_trash(self):
        for change in ('link', 'episodes', 'pause', 'trash'):
            item = self.record(sources=[dict(id='s', share_url='https://115.com/s/abc123', episodes='1-2')])
            def changed(*args, **kwargs):
                with self.mod.database() as db:
                    current = self.mod.get_item(db, item['id'])
                    if change == 'link': current['sources'][0]['share_url'] = 'https://115.com/s/new123'
                    if change == 'episodes': current['sources'][0]['episodes'] = '1-4'
                    if change == 'pause': current['tracking'] = False
                    db.execute('UPDATE items SET payload=?, deleted_at=? WHERE id=?',
                               (json.dumps(current), self.mod.now() if change == 'trash' else None, item['id']))
                return self.result()
            with self.subTest(change=change), patch.object(self.mod, 'read_share_episodes', side_effect=changed):
                self.mod.refresh_source_episodes(item['id'])
            with self.mod.database() as db:
                saved = json.loads(db.execute('SELECT payload FROM items WHERE id=?', (item['id'],)).fetchone()['payload'])
            self.assertEqual(saved['sources'][0]['episodes'], '1-4' if change == 'episodes' else '1-2')
            self.assertNotIn('episode_scan', saved['sources'][0])

    def test_scheduled_sync_scans_and_saves_even_if_tmdb_or_emby_fail(self):
        item = self.record(sources=[dict(id='s', share_url='https://115.com/s/abc123')])
        with patch.object(self.mod, 'read_share_episodes', return_value=self.result()) as scan:
            saved = self.perform_sync(error=IntegrationError('Emby 不可达'), metadata_error=IntegrationError('TMDB 不可达'))
        scan.assert_called_once()
        self.assertEqual(saved['sources'][0]['episodes'], '1-6')
        self.assertEqual(saved['emby']['state'], 'error')
        self.assertEqual(saved['library_episodes'], item['library_episodes'])

    def test_first_re0_binding_enqueues_scan_before_cms_execution(self):
        item = self.record()
        row = dict(slug='test-source', uploader='发布者', title='Test', resolution='4K', source='WEB',
                   resource_url='https://re0.me/resource/test-source', share_url='https://115.com/s/abc123',
                   code='', remark='', size='')
        cms = unittest.mock.Mock()
        cms.resolve.return_value = row
        with patch.object(self.mod, 'cms_client', return_value=cms), patch.object(self.mod, 'sync_cms_item'), patch.object(self.mod, 'run_cms_item'):
            response = self.client.post('/api/items/' + item['id'] + '/re0',
                json={'slug': 'test-source', 'uploader': '发布者'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.mod.start_source_refresh.assert_called_with(item['id'])
        with patch.object(self.mod, 'read_share_episodes', return_value=self.result()):
            self.mod.refresh_source_episodes(item['id'])
        self.assertEqual(self.saved(item)['sources'][-1]['episodes'], '1-6')

    def test_scan_is_read_only_and_uses_saved_source(self):
        item = self.record(sources=[dict(id='selected', share_url='https://115.com/s/abc123', code='abcd')], active_source_id='selected')
        before = self.client.get('/api/items').json
        result = dict(state='complete', episodes='1-6', episode_count=6)
        with patch.object(self.mod, 'read_share_episodes', return_value=result) as scan:
            response = self.client.post('/api/items/' + item['id'] + '/sources/selected/episodes',
                                       json={'share_url': 'https://evil.test'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['result'], result)
        self.assertEqual(scan.call_args.args, ('https://115.com/s/abc123', 'abcd'))
        self.assertEqual(self.client.get('/api/items').json, before)

    def test_movie_scan_api_and_background_save_detect_source_without_assuming_library(self):
        item = self.record(kind='movie', library_episodes='', sources=[dict(id='movie', share_url='https://115.com/s/film123')])
        result = self.result(episodes='1', episode_count=1)
        with patch.object(self.mod, 'read_share_episodes', return_value=result) as scan:
            response = self.client.post('/api/items/' + item['id'] + '/sources/movie/episodes', json={}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(scan.call_args.kwargs['media_type'], 'movie')
            self.assertEqual(self.saved(item)['sources'][0]['episodes'], '')
            self.mod.refresh_source_episodes(item['id'])
        saved = self.saved(item)
        self.assertEqual(saved['sources'][0]['episodes'], '1')
        self.assertEqual(saved['sources'][0]['episode_scan']['state'], 'complete')
        self.assertEqual(saved['library_episodes'], '')
        self.assertEqual(saved['events'][-1]['detail'], '网盘来源已检测到电影正片')

    def test_scanning_requires_admin_csrf_and_an_existing_source(self):
        item = self.record()
        path = '/api/items/' + item['id'] + '/sources/selected/episodes'
        with patch.object(self.mod, 'read_share_episodes') as scan:
            self.assertEqual(self.app.test_client().post(path, json={}).status_code, 401)
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
            self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 404)
            identity = dict(id='emby:s:u', username='user', provider='emby', role='user')
            with patch.object(self.mod, 'authenticated', return_value=True), patch.object(self.mod, 'principal', return_value=identity):
                self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 403)
            scan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
