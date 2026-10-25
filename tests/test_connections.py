"""Admin connection diagnostics use bounded read-only probes and do not expose secrets."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import Mock, patch

from integrations import IntegrationError
from cms_client import CMSClient


class ConnectionTests(unittest.TestCase):
    def test_native_cms_replaces_separate_re0_service(self):
        cfg = self.config()
        cfg['re0_via_cms'] = True
        services = self.mod.connection_services(cfg)
        self.assertEqual([row['id'] for row in services], ['emby','tmdb','cms'])
        self.assertEqual(services[-1]['name'], 'CloudMediaSync')
        self.assertIn('RE0', services[-1]['purpose'])
        self.assertNotEqual(self.mod.connection_signature(cfg), self.mod.connection_signature(dict(cfg,re0_via_cms=False)))

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='shiying-connections-')
        with patch.dict(os.environ, dict(DATA_DIR=cls.temp.name, SECRET_KEY=secrets.token_hex(32),
                        ADMIN_USERNAME='tester', ADMIN_PASSWORD='Connection-Test-12345', ENABLE_SCHEDULER='false')):
            spec = importlib.util.spec_from_file_location('connections_test_app', Path(__file__).parents[1] / 'app.py')
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)
        cls.mod.app.config['TESTING'] = True

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.mod.persist_config({})
        self.mod.CONNECTION_STATUS.update(signature='', checking=False, updated_monotonic=0, services=[])
        with self.mod.database() as db:
            db.execute('DELETE FROM attempts')
        self.client = self.mod.app.test_client()
        login = self.client.post('/api/login', json=dict(username='tester', password='Connection-Test-12345'))
        self.headers = {'X-CSRF-Token': login.json['csrf']}

    def config(self):
        cfg = dict(emby_url='http://emby.example:8096/emby', cms_url='http://nas.example:9526',
                   re0_gateway_url='http://gateway.example:9526', re0_gateway_token=self.mod.encrypt_secret('QUERY-SECRET'),
                   emby_api_key=self.mod.encrypt_secret('EMBY-SECRET'),
                   tmdb_credential=self.mod.encrypt_secret('TMDB-SECRET'),
                   cms_token=self.mod.encrypt_secret('CMS-SECRET'))
        self.mod.persist_config(cfg)
        return cfg

    def clients(self):
        return [patch.object(self.mod, name+'_client', return_value=Mock()) for name in ('tmdb','emby','cms')]

    def test_endpoints_require_authentication_and_csrf(self):
        anon = self.mod.app.test_client()
        self.assertEqual(anon.get('/api/connections').status_code, 401)
        self.assertEqual(anon.post('/api/connections/check', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/connections/check', json={}).status_code, 403)

    def test_initial_check_is_async_and_coalesces_polling(self):
        self.config()
        with patch.object(self.mod.threading, 'Thread') as thread:
            first = self.client.get('/api/connections')
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json['checking'])
            self.assertTrue(all(row['state'] == 'unknown' for row in first.json['services']))
            self.client.get('/api/connections')
            self.client.post('/api/connections/check', json={}, headers=self.headers)
            self.assertEqual(thread.call_count, 1)
            thread.return_value.start.assert_called_once()

    def test_real_probes_return_addresses_routes_and_no_credentials(self):
        cfg = self.config()
        with patch.object(self.mod.threading, 'Thread'):
            self.mod.current_connections()
        with patch.object(self.mod, 'tmdb_client') as tmdb, patch.object(self.mod, 'emby_client') as emby, patch.object(self.mod, 'cms_client') as cms, patch.object(self.mod, 'executor_client') as executor:
            self.mod.check_connections(cfg, self.mod.connection_signature(cfg))
            for factory in (tmdb, emby, cms, executor):
                factory.return_value.test.assert_called_once()
        result = self.client.get('/api/connections').json
        self.assertFalse(result['checking'])
        rows = {row['id']: row for row in result['services']}
        self.assertEqual(rows['emby']['address'], cfg['emby_url'])
        self.assertEqual(rows['tmdb']['address'], 'https://api.themoviedb.org/3')
        self.assertEqual(rows['cms']['address'], cfg['cms_url'])
        self.assertEqual(rows['cms']['route'], '拾影 → 我的 CMS → 115')
        self.assertEqual(rows['re0']['address'], cfg['re0_gateway_url'])
        self.assertEqual(rows['re0']['route'], '拾影 → 已授权 CMS → RE0')
        for row in rows.values():
            self.assertEqual(row['state'], 'connected')
            self.assertTrue(row['checked_at'])
        for secret in ('EMBY-SECRET','TMDB-SECRET','CMS-SECRET',cfg['cms_token']):
            self.assertNotIn(secret,json.dumps(result))

    def test_probe_failures_do_not_mark_configured_services_connected(self):
        cfg = self.config()
        for row in self.mod.connection_services(cfg):
            factory_name = {'re0':'cms', 'cms':'executor'}.get(row['id'], row['id']) + '_client'
            with self.subTest(target=row['id']), patch.object(self.mod, factory_name) as factory:
                factory.return_value.test.side_effect = IntegrationError('凭据无效')
                result = self.mod.probe_connection(row, cfg)
                self.assertEqual(result['state'], 'error')
                self.assertEqual(result['message'], '凭据无效')
                factory.return_value.test.side_effect = RuntimeError('SECRET-WITH-URL')
                self.assertNotIn('SECRET', self.mod.probe_connection(row,cfg)['message'])

    def test_unconfigured_services_are_not_probed_and_legacy_urls_are_sanitized(self):
        services = self.mod.connection_services(dict(emby_url='http://user:secret@example/?api_key=secret'))
        self.assertEqual(services[0]['address'], '')
        with patch.object(self.mod, 'tmdb_client') as factory:
            result = self.mod.probe_connection(services[1], {})
            self.assertEqual(result['state'], 'unconfigured')
            factory.assert_not_called()

    def test_cache_expires_and_manual_check_forces_refresh(self):
        cfg = self.config()
        with patch.object(self.mod.threading, 'Thread'):
            self.mod.current_connections()
        signature = self.mod.connection_signature(cfg)
        self.mod.CONNECTION_STATUS.update(checking=False, updated_monotonic=1000)
        with patch.object(self.mod.time, 'monotonic', return_value=1059), patch.object(self.mod.threading, 'Thread') as thread:
            self.mod.current_connections()
            thread.assert_not_called()
            self.mod.current_connections(force=True)
            self.assertEqual(thread.call_count,1)
        self.mod.CONNECTION_STATUS.update(checking=False, updated_monotonic=1000)
        with patch.object(self.mod.time, 'monotonic', return_value=1060), patch.object(self.mod.threading, 'Thread') as thread:
            self.mod.current_connections()
            self.assertEqual(thread.call_count,1)
        self.assertEqual(self.mod.CONNECTION_STATUS['signature'],signature)

    def test_changed_config_discards_old_results_and_invalidates_cache(self):
        old = self.config()
        with patch.object(self.mod.threading, 'Thread'):
            self.mod.current_connections()
            new = dict(old,emby_url='http://new.example:8096')
            self.mod.persist_config(new)
            current = self.mod.current_connections()
        self.assertEqual(current['services'][0]['address'],new['emby_url'])
        with patch.object(self.mod,'probe_connection',side_effect=lambda row,cfg:dict(row,state='connected')):
            self.mod.check_connections(old,self.mod.connection_signature(old))
        self.assertTrue(self.mod.CONNECTION_STATUS['checking'])
        self.assertEqual(self.mod.CONNECTION_STATUS['services'][0]['state'],'unknown')
        current['services'][0]['address']='MUTATED'
        self.assertEqual(self.mod.CONNECTION_STATUS['services'][0]['address'],new['emby_url'])

    def test_cms_status_check_is_bounded_and_does_not_resolve_or_unlock(self):
        client = CMSClient('http://cms.example:9526','SECRET')
        with patch('cms_client.fetch',return_value=dict(success=True,version=1,available=True)) as fetch:
            client.test()
            self.assertEqual(fetch.call_args.args[0],'http://cms.example:9526/api/shiying/status')
            self.assertEqual(fetch.call_args.kwargs['timeout'],12)
            self.assertIsNone(fetch.call_args.kwargs['body'])
        with patch.object(client,'call',return_value=dict(success=True,version=1,available=False)):
            with self.assertRaises(IntegrationError):client.test()
