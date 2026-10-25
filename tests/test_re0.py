import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
from test_integrations import IntegrationTests
from cms_client import CMSClient, canonical_re0_post_url
from integrations import IntegrationError


class RE0Tests(IntegrationTests):
    def test_legacy_cms_web_origin_is_repaired_without_changing_source_identity(self):
        item = self.record(sources=[dict(id='legacy', sharer='发布者甲',
                    post_url='https://re0.me/resource/115/same-source',
                    share_url='https://115.com/s/example')])
        with self.mod.database() as db:
            stored = json.loads(db.execute('SELECT payload FROM items WHERE id=?', (item['id'],)).fetchone()['payload'])
            stored['sources'][0]['post_url'] = 'https://hdhive.com/resource/115/same-source'
            db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(stored), item['id']))
            self.assertEqual(self.mod.migrate_re0_post_urls(db), 1)
            self.assertEqual(self.mod.migrate_re0_post_urls(db), 0)
        saved = self.client.get('/api/items').json['items'][0]
        self.assertEqual(saved['sources'], item['sources'])
        self.assertEqual(saved['updated_at'], item['updated_at'])
        row = self.row(resource_url='https://hdhive.com/resource/115/same-source')
        self.assertEqual(CMSClient.clean(row, item['tmdb'])['resource_url'], self.row()['resource_url'])
        for url in ('https://example.com/resource/115/same-source', 'https://hdhive.com/help',
                    'https://hdhive.com.evil.test/resource/115/same-source'):
            self.assertEqual(canonical_re0_post_url(url), url)

    def row(self, **changes):
        row = dict(slug='same-source', uploader='发布者甲', title='作品第一季 4K', remark='持续更新',
                   source='WEB', resolution='2160p', size='20GB', resource_url='https://re0.me/resource/115/same-source',
                   tmdb_id=123, media_type='tv', unlock_points=0, is_unlocked=False,
                   share_url='https://115.com/s/abc?password=xyz', code='xyz', can_bind=True)
        row.update(changes)
        return row

    def bind(self, item, row=None):
        cms = Mock(); cms.resolve.return_value = row or self.row()
        with patch.object(self.mod, 'cms_client', return_value=cms):
            response = self.client.post('/api/items/' + item['id'] + '/re0', json=dict(slug='same-source', uploader='发布者甲'), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['item']

    def test_binding_history_unbind_and_edits(self):
        item = self.record(sources=[dict(id='manual',sharer='旧发布者',share_url='https://115.com/s/old')])
        bound = self.bind(item)
        self.assertEqual(len(bound['sources']), 2)
        self.assertEqual(bound['re0']['uploader'], '发布者甲')
        self.assertEqual(bound['sources'][-1]['share_url'], self.row()['share_url'])
        # No source episode count inferred from title or aired episodes.
        self.assertEqual(bound['sources'][-1]['episodes'], '')
        response = self.client.put('/api/items/' + item['id'], json=bound, headers=self.headers)
        self.assertEqual(response.json['item']['re0']['slug'], 'same-source')
        rebound = self.bind(response.json['item'])
        self.assertEqual(len(rebound['sources']), 2)
        response = self.client.delete('/api/items/'+item['id']+'/re0', headers=self.headers)
        self.assertNotIn('re0', response.json['item'])
        self.assertEqual(len(response.json['item']['sources']), 2)

    def test_backup_restores_binding(self):
        item=self.bind(self.record())
        backup=self.client.get('/api/backup').json
        with self.mod.database() as db: db.execute('DELETE FROM items')
        result=self.client.post('/api/import',json=backup,headers=self.headers)
        self.assertEqual(result.status_code,200,result.json)
        restored=self.client.get('/api/items').json['items'][0]
        self.assertEqual(restored['re0']['slug'],item['re0']['slug'])
        self.assertEqual(restored['active_source_id'],restored['re0']['source_id'])

    def test_sync_preserves_links_and_concurrent_unbind(self):
        item = self.bind(self.record())
        self.configure()
        cfg = self.mod.integration_config(); cfg.update(re0_gateway_url='http://cms.local:9526', re0_gateway_token=self.mod.encrypt_secret('test-cms-token'))
        self.mod.persist_config(cfg)
        cms = Mock(); cms.resolve.side_effect = IntegrationError('查询失败')
        with patch.object(self.mod, 'cms_client', return_value=cms):
            synced = self.perform_sync()
        self.assertEqual([{k: v for k, v in source.items() if k != 'episode_scan'} for source in synced['sources']], item['sources'])
        self.assertEqual(synced['sources'][0]['episode_scan']['state'], 'error')
        self.assertEqual(synced['re0']['error'], '查询失败')
        # User unbinds while CMS request is in flight: stale result must not resurrect binding.
        def unbind(*args):
            self.client.delete('/api/items/'+item['id']+'/re0', headers=self.headers)
            return self.row(share_url='https://115.com/s/new')
        cms.resolve.side_effect = unbind
        with patch.object(self.mod, 'cms_client', return_value=cms):
            synced = self.perform_sync()
        self.assertNotIn('re0', synced)
        self.assertEqual([{k: v for k, v in source.items() if k != 'episode_scan'} for source in synced['sources']], item['sources'])

    def test_client_checks_identity_price_and_url(self):
        client = CMSClient('http://cms.example:9526','token')
        item = self.record()
        for row in [self.row(tmdb_id=999), self.row(uploader='别人'), self.row(share_url='https://evil.example/link'), self.row(unlock_points=10)]:
            with self.subTest(row=row), patch.object(client,'call',return_value=dict(success=True,data=row)):
                with self.assertRaises(IntegrationError): client.resolve(item,'same-source','发布者甲')
        with patch.object(client,'call',return_value=dict(success=True,data=self.row())):
            self.assertEqual(client.resolve(item,'same-source','发布者甲')['code'], 'xyz')

    def test_explicit_unlock_limit_and_invalid_inputs(self):
        item=self.record()
        cms=Mock(); cms.resolve.return_value=self.row(is_unlocked=True,unlock_points=10)
        with patch.object(self.mod,'cms_client',return_value=cms):
            for invalid in [-1, True, '10', 10**10]:
                response=self.client.post('/api/items/'+item['id']+'/re0',json=dict(slug='same-source',uploader='发布者甲',max_points=invalid),headers=self.headers)
                self.assertEqual(response.status_code,400)
            cms.resolve.assert_not_called()
            response=self.client.post('/api/items/'+item['id']+'/re0',json=dict(slug='same-source',uploader='发布者甲',max_points=10),headers=self.headers)
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual(cms.resolve.call_args.kwargs,dict(max_points=10))
        client=CMSClient('http://cms.example:9526','token')
        with patch.object(client,'call',return_value=dict(success=True,data=self.row(is_unlocked=True,unlock_points=10))) as call:
            client.resolve(item,'same-source','发布者甲',max_points=10)
            self.assertEqual(call.call_args.args[1]['max_points'],10)
            client.resolve(item,'same-source','发布者甲')
            self.assertNotIn('max_points',call.call_args.args[1])
        self.assertTrue(client.clean(self.row(unlock_points=10),item['tmdb'])['can_unlock'])
        self.assertFalse(client.clean(self.row(unlock_points=None),item['tmdb'])['can_unlock'])

    def test_config_secret_and_role_boundary(self):
        response=self.client.put('/api/integrations', json=dict(cms_url='http://cms.example:9526', cms_token='CMS-PRIVATE-TOKEN'),headers=self.headers)
        self.assertTrue(response.json['cms_configured'])
        for value in [response.text,self.mod.setting('integration_config'), self.client.get('/api/backup').text]:
            self.assertNotIn('CMS-PRIVATE-TOKEN',value)
        item=self.bind(self.record())
        identity=dict(id='emby:test:user',username='ordinary',provider='emby',role='user')
        with patch.object(self.mod,'authenticated',return_value=True),patch.object(self.mod,'principal',return_value=identity):
            for method in ['get','post','delete']:
                response=getattr(self.client,method)('/api/items/'+item['id']+'/re0',headers=self.headers)
                self.assertEqual(response.status_code,403)
            item['requesters']=[dict(user_id=identity['id'],username='ordinary',active=True,submitted_at=self.mod.now())]
            visible=self.mod.item_for_user(item)
            self.assertNotIn('re0',visible)
            self.assertNotIn('sources',visible)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.raw=dict(slug='slug',pan_type='115',title='第一季',user=dict(nickname='甲'),unlock_points=0)
        self.client=Mock(); self.client.search.return_value=dict(success=True,data=[self.raw]);self.client.unlock.return_value=dict(success=True,data=dict(full_url='https://115.com/s/abc'))
        self.ns=dict(json=json,hdhive_client=self.client,HDHIVE_WEB_BASE_URL='https://re0.me')
        exec((Path(__file__).parents[1]/'deploy/cms_bridge.py').read_text(),self.ns)
        self.params=dict(media_type='tv',tmdb_id=123,slug='slug',uploader='甲')

    def test_paid_unknown_or_changed_author_never_unlock(self):
        for change in [dict(unlock_points=2),dict(unlock_points=None),dict(user=dict(nickname='乙'))]:
            self.raw.update(change)
            with self.assertRaises(ValueError):self.ns['_shiying_operation']('/api/shiying/resolve',self.params)
        self.client.unlock.assert_not_called()

    def test_free_and_unlocked_resolve(self):
        result=self.ns['_shiying_operation']('/api/shiying/resolve',self.params)
        self.assertEqual(result['data']['share_url'],'https://115.com/s/abc')
        self.client.search.assert_called_with('tv','123')
        self.raw.update(unlock_points=10,is_unlocked=True)
        self.assertTrue(self.ns['_shiying_operation']('/api/shiying/resolve',self.params)['success'])

    def test_explicit_paid_unlock_and_price_changes(self):
        self.raw.update(unlock_points=10)
        for limit in [None, 9, True, '10', float('inf')]:
            with self.assertRaises(ValueError):
                self.ns['_shiying_operation']('/api/shiying/resolve',dict(self.params,max_points=limit))
        self.client.unlock.assert_not_called()
        result=self.ns['_shiying_operation']('/api/shiying/resolve',dict(self.params,max_points=10))
        self.assertTrue(result['data']['is_unlocked'])
        self.client.unlock.assert_called_once_with('slug')
        self.raw.update(is_unlocked=True)
        # Background check can read an already unlocked paid item without purchase authority.
        result=self.ns['_shiying_operation']('/api/shiying/resolve',self.params)
        self.assertTrue(result['success'])
        self.raw.update(is_unlocked=False,unlock_points=None)
        with self.assertRaises(ValueError):
            self.ns['_shiying_operation']('/api/shiying/resolve',dict(self.params,max_points=10))

    def test_auth_and_method_reject_before_external_access(self):
        for method,header,status in [('POST',b'Bearer bad',401),('GET',b'Bearer '+b'a'*64,405)]:
            messages=[]
            async def send(message):messages.append(message)
            async def receive():return dict(type='http.request',body=b'{}')
            with patch('pathlib.Path.read_text',return_value='a'*64):
                asyncio.run(self.ns['_shiying_bridge'](dict(path='/api/shiying/search',method=method,headers=[(b'authorization',header)]),receive,send))
            self.assertEqual(messages[0]['status'],status)
        self.client.search.assert_not_called()
