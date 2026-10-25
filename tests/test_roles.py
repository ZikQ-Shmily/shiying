"""Authentication, ownership and role boundaries with disposable storage and fake Emby."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import integrations


class RoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='shiying-roles-')
        # Start with the old schema, so every test also exercises the migration.
        with sqlite3.connect(Path(cls.temp.name) / 'media.sqlite3') as db:
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL)')
        with patch.dict(os.environ, dict(DATA_DIR=cls.temp.name, SECRET_KEY=secrets.token_hex(32),
                        ADMIN_USERNAME='maintenance', ADMIN_PASSWORD='Local-Temporary-12345', ENABLE_SCHEDULER='false')):
            spec = importlib.util.spec_from_file_location('role_test_app', Path(__file__).parents[1] / 'app.py')
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)
        cls.app = cls.mod.app
        cls.app.config['TESTING'] = True

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        with self.mod.database() as db:
            for table in ('items', 'sessions', 'attempts'):
                db.execute('DELETE FROM ' + table)
        self.mod.persist_config(dict(emby_url='http://emby.example.com:8096',
            emby_api_key=self.mod.encrypt_secret('service-api-secret'),
            tmdb_credential=self.mod.encrypt_secret('tmdb-test-secret'), auto_sync=False))
        self.users = {name: dict(Id=uid, Name=name, Policy=dict(IsAdministrator=admin, IsDisabled=False))
                      for name, uid, admin in [('alice', 'u1', False), ('bob', 'u2', False), ('owner', 'u3', True)]}
        self.server_id = 'serverA'
        self.user_api = Mock()
        def authenticate(name, password):
            if name not in self.users or password != 'emby-user-password':
                raise integrations.EmbyAuthError('账号或密码不正确')
            user = integrations.EmbyUserClient.validate_user(copy.deepcopy(self.users[name]))
            return dict(user=user, token='private-access-token-' + name, server_id=self.server_id)
        def user_by_id(uid):
            user = next(value for value in self.users.values() if value['Id'] == uid)
            return integrations.EmbyUserClient.validate_user(copy.deepcopy(user))
        self.user_api.authenticate.side_effect = authenticate
        self.user_api.user.side_effect = user_by_id
        self.auth_patch = patch.object(self.mod, 'EmbyUserClient', return_value=self.user_api)
        self.auth_constructor = self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.sync_patch = patch.object(self.mod, 'start_sync', return_value=False)
        self.sync_patch.start(); self.addCleanup(self.sync_patch.stop)
        self.tmdb = Mock()
        self.tmdb.tracking_metadata.return_value = dict(id=123, media_type='tv', title='测试剧集',
            original_title='Test', overview='简介', year=2026, status='Returning Series', total_episodes=4,
            poster_path='', air_dates={'1':'2020-01-01','2':'2020-01-02','3':'2099-01-01','4':''})
        self.tmdb_patch = patch.object(self.mod, 'tmdb_client', return_value=self.tmdb)
        self.tmdb_patch.start(); self.addCleanup(self.tmdb_patch.stop)
        self.admin, self.admin_headers = self.login('maintenance', provider='local')

    def login(self, name, provider='emby', **extra):
        client = self.app.test_client()
        response = client.post('/api/login', json=dict(username=name, provider=provider,
            password='Local-Temporary-12345' if provider == 'local' else 'emby-user-password', **extra))
        self.assertEqual(response.status_code, 200, response.json)
        return client, {'X-CSRF-Token': response.json['csrf']}

    def track(self, client, headers, **extra):
        response = client.post('/api/tmdb/track', json=dict(tmdb_id=123, media_type='tv', season=1, **extra), headers=headers)
        self.assertIn(response.status_code, (200, 201), response.json)
        return response.json

    def expire_check(self):
        with self.mod.database() as db:
            for row in db.execute('SELECT id, identity FROM sessions').fetchall():
                if row['identity']:
                    identity = json.loads(row['identity']); identity['checked_at'] = 0
                    db.execute('UPDATE sessions SET identity=? WHERE id=?', (json.dumps(identity), row['id']))

    def test_roles_come_from_emby_not_submitted_role(self):
        alice, _ = self.login('alice', role='admin', user_id='u3')
        owner, _ = self.login('owner', role='user')
        self.assertEqual(alice.get('/api/session').json['role'], 'user')
        self.assertEqual(owner.get('/api/session').json['role'], 'admin')
        self.assertEqual(owner.get('/api/integrations').status_code, 200)
        self.assertEqual(alice.get('/api/integrations').status_code, 403)

    def test_password_and_token_never_returned_or_stored_in_plaintext(self):
        client, _ = self.login('alice')
        with self.mod.database() as db:
            values = [row['identity'] for row in db.execute('SELECT identity FROM sessions')]
        stored = '\n'.join(values)
        self.assertNotIn('emby-user-password', stored)
        self.assertNotIn('private-access-token-alice', stored)
        identity = next(json.loads(value) for value in values if json.loads(value)['provider'] == 'emby')
        self.assertEqual(self.mod.decrypt_secret(identity['token']), 'private-access-token-alice')
        visible = client.get('/api/session').text + client.get_cookie('session').value
        for secret in ('private-access-token-alice', 'emby-user-password', 'service-api-secret', identity['token']):
            self.assertNotIn(secret, visible)
        self.auth_constructor.assert_called_with('http://emby.example.com:8096', device_id=identity['device_id'])

    def test_unconfigured_emby_keeps_local_maintenance_login(self):
        self.mod.persist_config({})
        client = self.app.test_client()
        self.assertEqual(client.get('/api/session').json, {'authenticated':False, 'emby_available':False})
        self.assertEqual(client.post('/api/login', json=dict(provider='emby', username='alice', password='x')).status_code, 503)
        self.assertEqual(self.admin.get('/api/session').json['role'], 'admin')

    def test_failed_and_disabled_logins_do_not_issue_sessions(self):
        client = self.app.test_client()
        self.users['alice']['Policy']['IsDisabled'] = True
        for name, password in [('alice','emby-user-password'), ('unknown','bad')]:
            response = client.post('/api/login', json=dict(provider='emby', username=name, password=password))
            self.assertEqual(response.status_code, 401)
            self.assertIsNone(client.get_cookie('session'))
        with self.mod.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 2)

    def test_ordinary_users_cannot_call_management_endpoints(self):
        alice, headers = self.login('alice')
        for path in ('/api/items?trash=1', '/api/integrations', '/api/connections', '/api/backup', '/api/users'):
            self.assertEqual(alice.get(path).status_code, 403, path)
        for method, path in [('post','/api/connections/check'), ('post','/api/items'), ('put','/api/items/anything'), ('delete','/api/items/anything'),
            ('post','/api/items/anything/restore'), ('post','/api/items/anything/check'), ('post','/api/items/anything/sync'),
            ('post','/api/import'), ('put','/api/integrations'), ('post','/api/sync'), ('post','/api/integrations/test'), ('post','/api/password')]:
            self.assertEqual(getattr(alice, method)(path, json={}, headers=headers).status_code, 403, path)

    def test_own_records_only_and_management_fields_are_removed(self):
        canonical = self.track(self.admin, self.admin_headers)['item']
        canonical.update(notes='private admin note', sources=[dict(id='s', sharer='private sharer', share_url='http://115.com/private', code='private-code', episodes='1-2')], active_source_id='s')
        self.assertEqual(self.admin.put('/api/items/' + canonical['id'], json=canonical, headers=self.admin_headers).status_code, 200)
        alice, headers = self.login('alice'); bob, _ = self.login('bob')
        self.assertEqual(alice.get('/api/items').json['items'], [])
        result = self.track(alice, headers, notes='spoof', requesters=[{'user_id':'u3'}])
        item = result['item']
        self.assertEqual(item['airing_today_count'], 0)
        self.assertEqual(item['airing_today_episodes'], '')
        for field in ('notes','sources','active_source_id','events','requesters','available_count','next_check',
                      'attention_reasons','needs_attention'):
            self.assertNotIn(field, item)
        for secret in ('private admin note','private sharer','private-code','115.com/private','maintenance'):
            self.assertNotIn(secret, json.dumps(item))
        self.assertEqual(bob.get('/api/items').json['items'], [])
        self.assertEqual(len(alice.get('/api/items').json['items']), 1)

    def test_attention_details_are_admin_only_and_user_cannot_set_check_date(self):
        alice, headers = self.login('alice')
        result = self.track(alice, headers, next_check='2020-01-01',
            attention_reasons=[dict(code='spoof', label='spoof', detail='spoof')], needs_attention=True)
        identifier = result['item']['id']
        with self.mod.database() as db:
            item = self.mod.get_item(db, identifier)
            self.assertEqual(item['next_check'], '')
            self.assertNotIn('attention_reasons', item)
            self.assertNotIn('needs_attention', item)
            item['tmdb']['metadata_error'] = 'private metadata failure'
            item['emby'] = dict(state='error', error='private library failure', checked_at='')
            item['re0'] = dict(error='private source failure')
            db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(item), identifier))
        managed = self.admin.get('/api/items').json['items'][0]
        self.assertTrue(managed['needs_attention'])
        self.assertEqual([reason['code'] for reason in managed['attention_reasons']], ['source_missing', 'sync_error'])
        for visible in (result['item'], alice.get('/api/items').json['items'][0]):
            self.assertNotIn('attention_reasons', visible)
            self.assertNotIn('needs_attention', visible)
            serialized = json.dumps(visible)
            for detail in ('private metadata failure', 'private library failure', 'private source failure'):
                self.assertNotIn(detail, serialized)

    def test_same_work_merges_requesters_and_seasons_stay_distinct(self):
        alice, ah = self.login('alice'); bob, bh = self.login('bob')
        first = self.track(alice, ah); second = self.track(bob, bh); again = self.track(alice, ah)
        self.assertEqual(first['item']['id'], second['item']['id'])
        self.assertTrue(second['added_request']); self.assertFalse(again['added_request'])
        records = self.admin.get('/api/items').json['items']
        self.assertEqual(len(records), 1)
        self.assertEqual({entry['username'] for entry in records[0]['requesters']}, {'alice','bob'})
        response = alice.post('/api/tmdb/track', json=dict(tmdb_id=123, media_type='tv', season=2), headers=ah)
        self.assertNotEqual(response.json['item']['id'], first['item']['id'])

    def test_withdraw_is_personal_and_resubmission_reactivates(self):
        alice, ah = self.login('alice'); bob, bh = self.login('bob'); outsider, oh = self.login('owner')
        item = self.track(alice, ah)['item']; self.track(bob, bh)
        url = '/api/items/' + item['id'] + '/withdraw'
        self.assertEqual(outsider.post(url, json={}, headers=oh).status_code, 404)
        self.assertEqual(alice.post(url, json={}, headers=ah).status_code, 200)
        self.assertEqual(alice.post(url, json={}, headers=ah).status_code, 200)
        self.assertFalse(alice.get('/api/items').json['items'][0]['request_active'])
        self.assertTrue(bob.get('/api/items').json['items'][0]['request_active'])
        self.assertTrue(self.track(alice, ah)['added_request'])
        canonical = self.admin.get('/api/items').json['items'][0]
        self.assertEqual(len(canonical['requesters']), 2)
        self.assertTrue(all(entry['active'] for entry in canonical['requesters']))

    def test_demoted_admin_loses_management_access_after_refresh(self):
        owner, headers = self.login('owner')
        self.users['owner']['Policy']['IsAdministrator'] = False; self.expire_check()
        self.assertEqual(owner.get('/api/integrations').status_code, 403)
        self.assertEqual(owner.get('/api/session').json['role'], 'user')
        self.assertEqual(owner.post('/api/sync', json={}, headers=headers).status_code, 403)
        self.assertEqual(self.auth_constructor.call_args.args[1], 'private-access-token-owner')

    def test_disabled_or_revoked_user_loses_session(self):
        client, _ = self.login('alice')
        self.users['alice']['Policy']['IsDisabled'] = True; self.expire_check()
        self.assertEqual(client.get('/api/items').status_code, 401)
        self.assertFalse(client.get('/api/session').json['authenticated'])
        self.users['alice']['Policy']['IsDisabled'] = False
        client, _ = self.login('alice')
        self.user_api.user.side_effect = integrations.EmbyAuthError('revoked'); self.expire_check()
        self.assertEqual(client.get('/api/items').status_code, 401)

    def test_emby_outage_fails_closed_and_keeps_session_for_recovery(self):
        client, _ = self.login('owner'); self.expire_check()
        self.user_api.user.side_effect = integrations.IntegrationError('offline')
        self.assertEqual(client.get('/api/integrations').status_code, 502)
        self.user_api.user.side_effect = None; self.user_api.user.return_value = self.users['owner']
        self.assertEqual(client.get('/api/integrations').status_code, 200)

    def test_renaming_keeps_ownership_and_server_change_isolated(self):
        alice, ah = self.login('alice'); original = self.track(alice, ah)['item']['id']
        self.users['alice']['Name'] = 'new-alice'; self.expire_check()
        self.assertEqual(alice.get('/api/session').json['username'], 'new-alice')
        self.assertEqual(alice.get('/api/items').json['items'][0]['id'], original)
        self.server_id = 'serverB'
        another, _ = self.login('alice')
        self.assertEqual(another.get('/api/items').json['items'], [])
        cfg = self.mod.integration_config(); cfg['emby_url'] = 'http://another.example.com'; self.mod.persist_config(cfg)
        self.assertEqual(alice.get('/api/items').status_code, 401)

    def test_user_csrf_and_logout_revoke_copied_cookie_and_remote_token(self):
        alice, ah = self.login('alice')
        copied = self.app.test_client(); copied.set_cookie('session', alice.get_cookie('session').value)
        self.assertEqual(alice.post('/api/tmdb/track', json={}).status_code, 403)
        self.assertEqual(alice.post('/api/logout', headers=ah).status_code, 200)
        self.user_api.logout.assert_called_once()
        self.assertEqual(copied.get('/api/items').status_code, 401)

    def test_admin_edit_and_backup_preserve_requester_history(self):
        alice, ah = self.login('alice'); self.track(alice, ah)
        item = self.admin.get('/api/items').json['items'][0]; original = copy.deepcopy(item['requesters'])
        item.update(notes='edited', requesters=[])
        updated = self.admin.put('/api/items/' + item['id'], json=item, headers=self.admin_headers).json['item']
        self.assertEqual(updated['requesters'], original)
        backup = self.admin.get('/api/backup').json
        with self.mod.database() as db: db.execute('DELETE FROM items')
        self.assertEqual(self.admin.post('/api/import', json=backup, headers=self.admin_headers).status_code, 200)
        self.assertEqual(alice.get('/api/items').json['items'][0]['id'], item['id'])
        self.assertEqual(self.admin.get('/api/items').json['items'][0]['requesters'], original)

    def test_user_directory_counts_server_scoped_active_requests(self):
        alice, ah = self.login('alice'); self.track(alice, ah)
        fake = Mock(); fake.users.return_value = [dict(id='u1', server_id='serverA', username='alice', is_admin=False, disabled=False),
                                                 dict(id='u1', server_id='other', username='other', is_admin=False, disabled=False)]
        with patch.object(self.mod, 'emby_client', return_value=fake):
            counts = [user['requests'] for user in self.admin.get('/api/users').json['users']]
        self.assertEqual(counts, [1, 0])


class EmbyAuthenticationProtocolTests(unittest.TestCase):
    def test_http_authentication_uses_post_password_and_client_header(self):
        raw = dict(User=dict(Id='u1', Name='alice', Policy={}), ServerId='s', AccessToken='user-token')
        with patch('integrations.fetch', return_value=raw) as fetch:
            result = integrations.EmbyUserClient('http://example.com:8096/emby', device_id='device1').authenticate('alice','pw-test')
        self.assertEqual(result['token'], 'user-token')
        self.assertEqual(fetch.call_args.args[0], 'http://example.com:8096/emby/Users/AuthenticateByName')
        self.assertEqual(fetch.call_args.kwargs['body'], {'Username':'alice','Pw':'pw-test'})
        self.assertIn('DeviceId="device1"', fetch.call_args.args[1]['X-Emby-Authorization'])
        self.assertNotIn('X-Emby-Token', fetch.call_args.args[1])

    def test_incomplete_disabled_or_wrong_user_is_rejected(self):
        for raw in ({}, dict(User=dict(Id='a', Name='x', Policy={}), AccessToken='x'),
                    dict(User=dict(Id='a', Name='x', Policy={'IsDisabled':True}), AccessToken='x', ServerId='s')):
            with patch('integrations.fetch', return_value=raw), self.assertRaises(integrations.EmbyAuthError):
                integrations.EmbyUserClient('http://example.com').authenticate('x','x')
        with patch('integrations.fetch', return_value=dict(Id='another', Name='x', Policy={})), self.assertRaises(integrations.EmbyAuthError):
            integrations.EmbyUserClient('http://example.com','token').user('requested')

    def test_http_400_password_failure_is_countable_auth_failure(self):
        opener = Mock(); opener.open.side_effect = HTTPError('http://example.com/Users/AuthenticateByName',400,'bad',{},None)
        with patch('integrations.build_opener', return_value=opener), self.assertRaises(integrations.EmbyAuthError):
            integrations.EmbyUserClient('http://example.com').authenticate('x','secret')

    def test_directory_omits_emby_private_fields_and_resolves_server_id(self):
        client = integrations.EmbyClient('http://example.com','service-key')
        client.get = Mock(side_effect=[[dict(Id='u1', Name='alice', HasPassword=True, Configuration={'ProfilePin':'private'}, Policy={'IsAdministrator':True})], {'Id':'server'}])
        self.assertEqual(client.users(), [dict(id='u1', server_id='server', username='alice', is_admin=True, disabled=False)])


if __name__ == '__main__':
    unittest.main()
