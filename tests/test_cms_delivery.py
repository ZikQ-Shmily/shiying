import copy
import json
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_integrations import IntegrationTests
from cms_client import CMSExecutor
from integrations import IntegrationError


class DeliveryTests(IntegrationTests):
    def test_manual_bind_runs_once_but_reconciliation_does_not(self):
        item = self.prepare()
        self.executor.run.side_effect = lambda payload, **kw: dict(request_id=payload['request_id'],
            state='queued', message='等待立即执行')
        query = Mock()
        query.resolve.return_value = dict(slug='fixed-source', uploader='发布者甲', title='作品',
            share_url='https://115.com/s/abc', code='', resolution='2160p', source='WEB', remark='', size='',
            resource_url='https://re0.me/resource/115/fixed-source')
        with patch.object(self.mod, 'executor_client', return_value=self.executor), patch.object(self.mod, 'cms_client', return_value=query):
            response = self.client.post('/api/items/'+item['id']+'/re0', json=dict(slug='fixed-source',uploader='发布者甲'), headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['item']['cms_run']['state'], 'queued')
            self.executor.run.assert_called_once()
            self.assertTrue(self.executor.run.call_args.kwargs['start'])
            self.mod.sync_cms_item(item['id'], force=True)
            self.executor.run.assert_called_once()
            self.client.post('/api/items/'+item['id']+'/cms/run', json={}, headers=self.headers)
        first, second = self.executor.run.call_args_list
        self.assertEqual(first.args[0]['request_id'], second.args[0]['request_id'])

    def test_immediate_failure_keeps_subscription_and_does_not_claim_completion(self):
        item = self.prepare()
        self.executor.run.side_effect = IntegrationError('CMS 暂不可用')
        with patch.object(self.mod, 'executor_client', return_value=self.executor):
            result = self.client.post('/api/items/'+item['id']+'/cms/run', json={}, headers=self.headers)
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json['item']['cms_run']['state'], 'unconfirmed')
            self.assertEqual(result.json['item']['cms_delivery']['state'], 'submitted')
            self.assertEqual(result.json['item']['library_count'], 1)
            self.executor.run.side_effect = None
            self.executor.run.return_value = None
            result = self.client.get('/api/items/'+item['id']+'/cms/run')
            self.assertEqual(result.json['item']['cms_run']['state'], 'error')
        actor = dict(id='emby:test:ordinary', username='user', provider='emby', role='user')
        with patch.object(self.mod, 'authenticated', return_value=True), patch.object(self.mod, 'principal', return_value=actor):
            for method in ('get', 'post'):
                self.assertEqual(getattr(self.client, method)('/api/items/'+item['id']+'/cms/run', headers=self.headers).status_code, 403)

    def test_binding_while_paused_saves_source_without_starting_transfer(self):
        item = self.prepare()
        with self.mod.database() as db:
            current = self.mod.get_item(db, item['id'])
            current['tracking'] = False
            db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(current), item['id']))
        query = Mock()
        query.resolve.return_value = dict(slug='fixed-source', uploader='发布者甲', title='作品',
            share_url='https://115.com/s/abc', code='', resolution='2160p', source='WEB', remark='', size='',
            resource_url='https://re0.me/resource/115/fixed-source')
        with patch.object(self.mod, 'executor_client', return_value=self.executor), patch.object(self.mod, 'cms_client', return_value=query):
            response = self.client.post('/api/items/'+item['id']+'/re0', json=dict(slug='fixed-source',uploader='发布者甲'), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['item']['cms_delivery']['state'], 'paused')
        self.executor.run.assert_not_called()

    def test_native_re0_uses_only_own_cms_and_forces_reconciliation(self):
        item = self.prepare()
        old = self.mod.integration_config()
        with patch.object(self.mod, 'executor_client', return_value=self.executor):
            self.mod.sync_cms_item(item['id'])
            cfg = dict(old, re0_via_cms=True)
            self.mod.persist_config(cfg)
            self.mod.sync_cms_item(item['id'])
        self.assertEqual(self.executor.subscribe.call_count, 2)
        self.assertEqual(self.executor.subscribe.call_args.args[0]['expected_gateway_url'], cfg['cms_url'])
        query = self.mod.cms_client(cfg)
        self.assertEqual(query.url, cfg['cms_url'])
        self.assertEqual(query.token, 'EXECUTOR-SECRET')
        self.assertTrue(self.mod.executor_client(cfg).native_re0)
        self.assertEqual(self.saved(item)['re0']['uploader'], '发布者甲')
        self.assertTrue(self.client.get('/api/integrations').json['re0_configured'])

    def prepare(self):
        cfg = dict(cms_url='http://executor.example:9527', cms_token=self.mod.encrypt_secret('EXECUTOR-SECRET'),
                   re0_gateway_url='http://gateway.example:9526', re0_gateway_token=self.mod.encrypt_secret('QUERY-SECRET'))
        self.mod.persist_config(cfg)
        item = self.record()
        with self.mod.database() as db:
            raw = self.mod.get_item(db, item['id'])
            raw['re0'] = dict(slug='fixed-source', uploader='发布者甲')
            db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(raw), item['id']))
        self.executor = Mock()
        self.executor.subscribe.side_effect = lambda payload: dict(state='submitted', subscription_id='81',
            active=payload['active'], native_status=1 if payload['active'] else 0)
        return item

    def saved(self, item):
        with self.mod.database() as db:
            return self.mod.get_item(db, item['id'], True)

    def test_delivery_is_idempotent_and_uses_executor_not_query_credentials(self):
        item = self.prepare()
        with patch.object(self.mod, 'executor_client', return_value=self.executor):
            self.mod.sync_cms_item(item['id'])
            self.mod.sync_cms_item(item['id'])
        self.executor.subscribe.assert_called_once()
        payload = self.executor.subscribe.call_args.args[0]
        self.assertEqual((payload['slug'], payload['uploader'], payload['season']), ('fixed-source', '发布者甲', 1))
        self.assertEqual(payload['expected_gateway_url'], 'http://gateway.example:9526')
        self.assertNotIn('max_points', payload)
        self.assertEqual(self.saved(item)['cms_delivery']['state'], 'submitted')
        cfg = self.mod.integration_config()
        self.assertEqual(self.mod.cms_client(cfg).url, cfg['re0_gateway_url'])
        self.assertEqual(self.mod.cms_client(cfg).token, 'QUERY-SECRET')
        self.assertEqual(self.mod.executor_client(cfg).token, 'EXECUTOR-SECRET')

    def test_pause_unbind_trash_restore_and_redelivery(self):
        item = self.prepare()
        with patch.object(self.mod, 'executor_client', return_value=self.executor):
            self.mod.sync_cms_item(item['id'])
            value = self.saved(item)
            response = self.client.put('/api/items/'+item['id'], json=dict(value, tracking=False), headers=self.headers)
            self.assertEqual(response.json['item']['cms_delivery']['state'], 'paused')
            self.assertFalse(self.executor.subscribe.call_args.args[0]['active'])
            value['tracking'] = True
            self.client.put('/api/items/'+item['id'], json=value, headers=self.headers)
            self.client.delete('/api/items/'+item['id'], headers=self.headers)
            self.assertFalse(self.executor.subscribe.call_args.args[0]['active'])
            self.client.post('/api/items/'+item['id']+'/restore', json={}, headers=self.headers)
            self.assertTrue(self.executor.subscribe.call_args.args[0]['active'])
            self.client.delete('/api/items/'+item['id']+'/re0', headers=self.headers)
            self.assertFalse(self.executor.subscribe.call_args.args[0]['active'])
            self.assertNotIn('re0', self.saved(item))
            self.assertEqual(self.saved(item)['cms_delivery']['slug'], 'fixed-source')

    def test_failure_keeps_binding_and_retries_without_success_badge(self):
        item = self.prepare()
        self.executor.subscribe.side_effect = IntegrationError('执行端不可达')
        with patch.object(self.mod, 'executor_client', return_value=self.executor):
            self.mod.sync_cms_item(item['id'])
            saved = self.saved(item)
            self.assertEqual(saved['re0']['slug'], 'fixed-source')
            self.assertEqual(saved['cms_delivery']['state'], 'error')
            self.assertTrue(self.mod.decorate(saved)['needs_attention'])
            self.mod.sync_cms_item(item['id'])
        self.assertEqual(self.executor.subscribe.call_count, 2)

    def test_late_delivery_is_followed_by_latest_pause(self):
        item = self.prepare()
        def subscribe(payload):
            if payload['active']:
                with self.mod.database() as db:
                    current = self.mod.get_item(db, item['id']); current['tracking'] = False
                    db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(current), item['id']))
            return dict(subscription_id='81', active=payload['active'])
        self.executor.subscribe.side_effect = subscribe
        with patch.object(self.mod, 'executor_client', return_value=self.executor):
            self.mod.sync_cms_item(item['id'])
        self.assertEqual([c.args[0]['active'] for c in self.executor.subscribe.call_args_list], [True, False])
        self.assertEqual(self.saved(item)['cms_delivery']['state'], 'paused')

    def test_delivery_credentials_private_and_endpoint_admin_only(self):
        item = self.prepare()
        result = self.client.get('/api/integrations')
        self.assertTrue(result.json['re0_configured'])
        for output in (result.text, self.client.get('/api/backup').text, self.mod.setting('integration_config')):
            for secret in ('QUERY-SECRET', 'EXECUTOR-SECRET'):
                self.assertNotIn(secret, output)
        actor = dict(id='emby:test:ordinary', username='user', provider='emby', role='user')
        with patch.object(self.mod, 'authenticated', return_value=True), patch.object(self.mod, 'principal', return_value=actor):
            self.assertEqual(self.client.post('/api/items/'+item['id']+'/cms',json={},headers=self.headers).status_code,403)


class ExecutorClientTests(unittest.TestCase):
    def test_native_status_rejects_remote_gateway_disguised_as_local(self):
        client = CMSExecutor('http://executor.example:9527', 'secret', native_re0=True)
        status = dict(success=True, version=1, executor=True, available=True, source_guard=True)
        with patch.object(client, 'call', return_value=dict(status, query_mode='remote')):
            with self.assertRaises(IntegrationError):
                client.test()
        with patch.object(client, 'call', return_value=dict(status, query_mode='local')):
            self.assertTrue(client.test())

    def test_wrong_publisher_and_partial_ack_are_not_success(self):
        client = CMSExecutor('http://executor.example:9527', 'secret')
        payload = dict(active=True, uploader='甲', slug='fixed', season=2)
        for row in [dict(active=False), dict(active=True, state='submitted', uploader='乙', slug='fixed', season=2),
                    dict(active=True,state='submitted',uploader='甲',slug='fixed',season=1)]:
            with self.subTest(row=row), patch.object(client,'call',return_value=dict(data=row)):
                with self.assertRaises(IntegrationError): client.subscribe(payload)


class ExecutorAddonTests(unittest.TestCase):
    def setUp(self):
        self.registry = dict(entries={}, owners={})
        self.rows, self.filters, self.log = [], {}, []
        def config_get(key): return copy.deepcopy(self.registry)
        def config_set(key,value): self.registry=copy.deepcopy(value);return True
        def find(**kw):return copy.deepcopy(self.rows)
        def add(payload):self.log.append('create-paused');self.rows.append(dict(payload,id=81));return True,''
        def status(ids,value):self.log.append(('status',value));self.rows[0]['status']=value;return True,''
        def rules(**kw):self.log.append('rules');self.filters=kw;return True,''
        self.ns=dict(json=json,_cms_config_get_json=config_get,_cms_config_set_json=config_set,
            _cms_find_submedia_rows_db=find,_cms_add_submedia_db=add,_cms_update_submedia_status_db=status,
            _set_enhanced_sub_item=rules,_get_enhanced_sub_item=lambda **kw:self.filters,
            _subscription_fetch_hdhive_merged=Mock(),_has_hdhive_lookup_source=lambda:False,
            _patch_share_down_service=Mock(),_log=Mock(),_sub_sync_patched=False)
        exec((Path(__file__).parents[1]/'deploy/cms_executor.py').read_text(),self.ns)
        self.local_gateway = self.ns['_sx_gateway']
        self.local_config = self.ns['_sx_gateway_config']
        self.ns['_sx_gateway_config']=lambda:dict(gateway_url='http://internal:9526',public_gateway_url='http://gateway:9526')
        self.ns['_sx_guards_ready']=lambda:True
        self.ns['_sx_established']=lambda:False
        self.ns['_sx_mark_established']=lambda:None
        def resolve(action,payload=None):
            self.log.append(('gateway',payload))
            return dict(success=True,data=dict(slug=payload['slug'],uploader=payload['uploader'],title='作品',share_url='https://115.com/s/abc'))
        self.ns['_sx_gateway']=Mock(side_effect=resolve)
        self.params=dict(item_id='12345678-1234-1234-1234-123456789abc', media_type='tv',tmdb_id=123,
            title='作品',year=2026,season=1,slug='fixed',uploader='甲',active=True,revision=1,
            expected_gateway_url='http://gateway:9526')

    def submit(self,**changes):return self.ns['_sx_subscribe'](dict(self.params,**changes))

    def run_once(self, **changes):
        return self.ns['_sx_run_once'](dict(self.params, request_id='11111111-1111-1111-1111-111111111111', **changes))

    def test_immediate_job_is_idempotent_exact_source_only_and_not_part_of_subscribe(self):
        starter = self.ns['_sx_start_run_worker'] = Mock()
        transfer = self.ns['_sx_transfer'] = Mock(return_value=('submitted','已提交转存'))
        self.submit()
        starter.assert_not_called()
        first = self.run_once()
        self.assertEqual(first['data']['run']['state'], 'queued')
        self.run_once()
        starter.assert_called_once()
        self.ns['_sx_run_worker'](self.params['item_id'],first['data']['run']['request_id'])
        transfer.assert_called_once_with('https://115.com/s/abc')
        result = self.run_once()
        self.assertEqual(result['data']['run']['state'], 'submitted')
        transfer.assert_called_once()
        self.assertNotIn('max_points',self.ns['_sx_gateway'].call_args.args[1])
        self.assertNotIn('share_url', result['data']['run'])

    def test_paused_or_changed_source_cancels_queued_job_and_new_binding_can_queue(self):
        self.ns['_sx_start_run_worker'] = Mock()
        transfer = self.ns['_sx_transfer'] = Mock()
        self.submit()
        first = self.run_once()
        self.submit(active=False, revision=2)
        self.ns['_sx_run_worker'](self.params['item_id'], first['data']['run']['request_id'])
        transfer.assert_not_called()
        self.assertEqual(self.registry['entries'][self.params['item_id']]['run']['state'], 'cancelled')
        with self.assertRaises(ValueError):self.run_once()
        self.submit(slug='new-source',uploader='乙',revision=3)
        result=self.ns['_sx_run_once'](dict(self.params,slug='new-source',uploader='乙',request_id='22222222-2222-2222-2222-222222222222'))
        self.assertEqual(result['data']['run']['state'],'queued')

    def test_read_failure_never_submits_and_restart_is_unknown(self):
        self.ns['_sx_start_run_worker'] = Mock()
        transfer = self.ns['_sx_transfer'] = Mock()
        self.submit()
        first = self.run_once()
        self.ns['_sx_gateway'].side_effect=RuntimeError('upstream offline')
        self.ns['_sx_run_worker'](self.params['item_id'],first['data']['run']['request_id'])
        transfer.assert_not_called()
        entry=self.registry['entries'][self.params['item_id']]
        self.assertEqual(entry['run']['state'],'error')
        entry['run']['state']='running'
        self.assertEqual(self.ns['_sx_run_status'](entry)['state'],'unknown')
        with self.assertRaises(ValueError):self.ns['_sx_transfer_url']({'share_url':'https://evil.example/s/a'})
        self.assertEqual(self.ns['_sx_transfer_url']({'share_url':'https://115.com/s/a','code':'x y'}), 'https://115.com/s/a?password=x+y')

    def test_local_query_uses_native_oauth_without_http_or_remote_fallback(self):
        self.ns['_sx_gateway_config'] = lambda: dict(query_mode='local', public_gateway_url='http://own:9527')
        native = Mock(return_value=dict(success=True, data=[]))
        self.ns['_shiying_operation'] = native
        with patch('urllib.request.build_opener') as opener:
            self.local_gateway('search', {'tmdb_id':123,'media_type':'tv'})
            native.assert_called_once_with('/api/shiying/search', {'tmdb_id':123,'media_type':'tv'})
            opener.assert_not_called()
            native.side_effect = RuntimeError('own authorization unavailable')
            with self.assertRaises(RuntimeError):
                self.local_gateway('resolve', {'slug':'fixed','uploader':'甲'})
            opener.assert_not_called()

    def test_local_mode_still_preserves_exact_source_rules_and_native_subscription(self):
        self.ns['_sx_gateway_config'] = lambda: dict(query_mode='local', gateway_url='http://own:9527')
        self.ns['_shiying_operation'] = Mock(return_value=dict(success=True, data=dict(slug='fixed',uploader='甲')))
        self.ns['_sx_gateway'] = self.local_gateway
        result = self.submit(expected_gateway_url='http://own:9527')
        self.assertEqual(result['data']['subscription_id'], '81')
        self.assertEqual(self.filters['authors'], ['甲'])
        self.assertEqual(self.rows[0]['status'], 1)
        self.assertNotIn('max_points', self.ns['_shiying_operation'].call_args.args[1])

    def test_rules_precede_activation_and_duplicate_submit_is_one_record(self):
        result=self.submit()
        self.assertEqual(result['data']['subscription_id'],'81')
        self.assertLess(self.log.index('rules'), self.log.index('create-paused'))
        self.assertLess(self.log.index('create-paused'), self.log.index(('status',1)))
        self.submit(revision=2)
        self.assertEqual(len(self.rows),1)
        self.assertEqual(self.filters['sources'],['hdhive_author'])
        self.assertEqual(self.filters['authors'],['甲'])
        self.assertNotIn('max_points',self.ns['_sx_gateway'].call_args.args[1])

    def test_existing_foreign_subscription_and_wrong_gateway_not_overwritten(self):
        self.rows.append(dict(id=9,tmdb_id='123',type='tv',status=1))
        with self.assertRaises(ValueError):self.submit()
        self.assertEqual(self.registry['entries'],{})
        self.assertEqual(self.rows[0]['id'],9)
        self.rows.clear()
        with self.assertRaises(ValueError):self.submit(expected_gateway_url='http://wrong:9526')
        self.assertEqual(self.registry['entries'],{})

    def test_seasons_keep_exact_sources_and_pause_only_one(self):
        self.submit()
        second='87654321-1234-1234-1234-123456789abc'
        self.submit(item_id=second,season=2,slug='season-two',uploader='乙',revision=2)
        self.assertEqual(set(self.filters['authors']),{'甲','乙'})
        self.submit(active=False,revision=3)
        self.assertEqual(self.rows[0]['status'],1)
        merged=self.ns['_subscription_fetch_hdhive_merged']('作品',[dict(type='tv',tmdb_id='123')],mode='author')
        self.assertEqual([r['uploader'] for r in merged['115']],['乙'])
        self.submit(item_id=second,season=2,slug='season-two',uploader='乙',active=False,revision=4)
        self.assertEqual(self.rows[0]['status'],0)

    def test_stale_revision_and_failed_query_never_fall_back(self):
        self.submit(revision=5)
        with self.assertRaises(ValueError):self.submit(active=False,revision=4)
        self.ns['_sx_gateway'].side_effect=RuntimeError('query offline')
        self.assertEqual(self.ns['_subscription_fetch_hdhive_merged']('作品',[dict(type='tv',tmdb_id='123')],mode='author'),{})
        self.ns['_sx_original_fetch'].assert_not_called()
        self.assertEqual(self.rows[0]['status'],1)

    def test_unreadable_registry_and_missing_guard_never_activate_or_fall_back(self):
        self.ns['_sx_guards_ready']=lambda:False
        with self.assertRaises(RuntimeError):self.submit()
        self.assertEqual(self.rows,[])
        self.ns['_sx_established']=lambda:True
        self.ns['_cms_config_get_json']=lambda key:None
        with self.assertRaises(RuntimeError):
            self.ns['_subscription_fetch_hdhive_merged']('作品',[dict(type='tv',tmdb_id='123')],mode='author')
        self.ns['_sx_original_fetch'].assert_not_called()

    def test_source_guard_blocks_native_and_other_virtual_sources(self):
        self.submit()
        for before in (True,False):
            class Scraper:
                channel='native-channel'
                def search(self,q=None,*args,**kwargs):return ['unrelated']
                def search_all(self,q=None,*args,**kwargs):return ['unrelated']
            module=types.ModuleType('app.modules.tg.tg_scraper');module.TelegramScraper=Scraper
            self.ns.update(_sx_native_tuple=before,_sub_sync_patched=True,
                _subscription_sync_active=lambda:True,
                _subscription_rows_for_filter=lambda q:[dict(type='tv',tmdb_id='123')],
                SUB_SYNC_CHANNEL_TO_SOURCE={'author-channel':'hdhive_author'})
            with patch.dict('sys.modules',{'app.modules.tg.tg_scraper':module}):
                self.ns['_patch_share_down_service'](None)
            scraper=Scraper()
            self.assertEqual(scraper.search('作品'),([],None) if before else [])
            self.assertEqual(scraper.search_all('作品'),[])
            scraper.channel='author-channel'
            self.assertEqual(scraper.search('作品'),['unrelated'])


if __name__=='__main__':unittest.main()
