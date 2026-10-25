"""Mail delivery uses fake SMTP servers and a disposable application database."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import secrets
import smtplib
import tempfile
import unittest
from unittest.mock import Mock, patch

import email_notifications as mail


class EmailNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='shiying-mail-tests-')
        with patch.dict(os.environ, dict(DATA_DIR=cls.temp.name, SECRET_KEY=secrets.token_hex(32),
                ADMIN_USERNAME='test-admin', ADMIN_PASSWORD='Test-Mail-Password-2026', ENABLE_SCHEDULER='false')):
            spec = importlib.util.spec_from_file_location('email_test_app', Path(__file__).parents[1] / 'app.py')
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)
        cls.mod.app.config['TESTING'] = True

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        with self.mod.database() as db:
            for table in ('items', 'sessions', 'attempts', 'email_outbox'):
                db.execute('DELETE FROM ' + table)
            db.execute("DELETE FROM settings WHERE key IN ('notification_config','notification_delivery_status')")
        self.mod.persist_config(dict(emby_url='http://emby.example.com:8096',
            emby_api_key=self.mod.encrypt_secret('fake-service-key'),
            tmdb_credential=self.mod.encrypt_secret('fake-tmdb-key'), auto_sync=False))
        user_api = Mock()
        user_api.authenticate.side_effect = lambda name, password: dict(user=dict(
            Id=name, Name=name, Policy=dict(IsAdministrator=name == 'owner', IsDisabled=False)),
            token='fake-user-token', server_id='test-server')
        tmdb = Mock()
        tmdb.tracking_metadata.side_effect = lambda kind, identifier, season: dict(id=identifier,
            media_type=kind, title='<测试剧集>', original_title='Test', overview='简介', year=2026,
            status='Returning Series', total_episodes=12, poster_path='', air_dates={'1':'2020-01-01'})
        for name, options in [('EmbyUserClient', {'return_value':user_api}), ('tmdb_client', {'return_value':tmdb}),
                              ('start_sync', {'return_value':False}), ('start_email_delivery', {'return_value':False})]:
            mocker = patch.object(self.mod, name, **options)
            result = mocker.start(); self.addCleanup(mocker.stop)
            if name == 'start_email_delivery': self.worker = result
        self.admin, self.headers = self.login('test-admin', 'local')
        self.alice, self.alice_headers = self.login('alice')

    def login(self, name, provider='emby'):
        client = self.mod.app.test_client()
        response = client.post('/api/login', json=dict(provider=provider, username=name,
            password='Test-Mail-Password-2026' if provider == 'local' else 'fake-password'))
        self.assertEqual(response.status_code, 200, response.json)
        return client, {'X-CSRF-Token':response.json['csrf']}

    def config(self, **changes):
        value = dict(enabled=True, smtp_host='smtp.example.com', smtp_port=465, security='ssl',
            smtp_username='sender@example.com', smtp_password='private-smtp-code',
            from_email='sender@example.com', to_email='owner@example.com', site_url='https://sy.example.com')
        value.update(changes)
        return value

    def save(self, **changes):
        response = self.admin.put('/api/notifications', json=self.config(**changes), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def track(self, client=None, headers=None, **changes):
        data = dict(media_type='tv', tmdb_id=123, season=1); data.update(changes)
        response = (client or self.alice).post('/api/tmdb/track', json=data, headers=headers or self.alice_headers)
        self.assertIn(response.status_code, (200,201), response.json)
        return response.json

    def jobs(self):
        with self.mod.database() as db:
            return [dict(row) for row in db.execute('SELECT * FROM email_outbox ORDER BY created_at,id')]

    def test_disabled_by_default_and_no_backfill(self):
        self.assertFalse(self.admin.get('/api/notifications').json['enabled'])
        self.track()
        self.assertEqual(self.jobs(), [])
        self.save()
        self.assertEqual(self.jobs(), [])

    def test_secret_is_encrypted_retained_and_explicitly_cleared(self):
        public = self.save()
        self.assertTrue(public['has_password'])
        stored = self.mod.setting('notification_config')
        self.assertNotIn('private-smtp-code', stored + json.dumps(public))
        self.assertNotIn('smtp_password', public)
        config = self.mod.notification_config()
        self.assertEqual(self.mod.decrypt_secret(config['smtp_password']), 'private-smtp-code')
        response = self.admin.put('/api/notifications', json=dict(smtp_password=''), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mod.notification_config()['smtp_password'], config['smtp_password'])
        response = self.admin.put('/api/notifications', json=dict(enabled=False,clear_password=True), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['has_password'])

    def test_changed_smtp_identity_requires_new_secret(self):
        self.save()
        response = self.admin.put('/api/notifications', json=dict(smtp_host='different.example.com'), headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.mod.notification_config()['smtp_host'], 'smtp.example.com')
        response = self.admin.put('/api/notifications', json=dict(enabled=False,smtp_host='different.example.com'), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['has_password'])

    def test_invalid_configuration_rejected_atomically(self):
        self.save()
        previous = self.mod.setting('notification_config')
        for changes in [dict(to_email='a@example.com\r\nBcc: thief@example.com'),dict(to_email='a@example.com,b@example.com'),
                        dict(smtp_host='https://smtp.example.com:465'),dict(smtp_port=0),dict(smtp_port=True),
                        dict(security='none'),dict(enabled='true'),dict(site_url='https://user:password@example.com'),
                        dict(site_url='javascript:alert(1)'),dict(smtp_password='x\nsecret')]:
            response = self.admin.put('/api/notifications', json=changes, headers=self.headers)
            self.assertEqual(response.status_code, 400, (changes,response.json))
            self.assertEqual(self.mod.setting('notification_config'), previous)

    def test_admin_only_and_csrf_protected(self):
        anonymous = self.mod.app.test_client()
        for client, headers, code in [(anonymous,{},401),(self.alice,self.alice_headers,403)]:
            self.assertEqual(client.get('/api/notifications').status_code, code)
            for method,path in [('put','/api/notifications'),('post','/api/notifications/test'),('post','/api/notifications/retry')]:
                self.assertEqual(getattr(client,method)(path,json={},headers=headers).status_code,code)
        self.assertEqual(self.admin.put('/api/notifications',json=self.config()).status_code,403)
        self.assertEqual(self.admin.post('/api/notifications/test',json={}).status_code,403)

    def test_new_requests_notify_once_per_user_and_season(self):
        self.save()
        first = self.track(); self.track()
        self.assertEqual(len(self.jobs()), 1)
        self.assertFalse(self.track()['added_request'])
        bob,bh = self.login('bob'); self.track(bob,bh)
        self.track(season=2)
        self.assertEqual(len(self.jobs()),3)
        payload = json.loads(self.jobs()[0]['payload'])
        self.assertIn(payload['username'], ('alice','bob'))
        self.assertEqual(payload['title'],'<测试剧集>')
        self.assertNotIn('smtp_password',payload)
        self.assertEqual(self.admin.get('/api/items').json['items'][0]['tmdb']['id'],123)
        self.alice.post('/api/items/'+first['item']['id']+'/withdraw', json={}, headers=self.alice_headers)
        self.track()
        self.assertEqual(len(self.jobs()),4)

    def test_admin_additions_and_sync_do_not_notify(self):
        self.save()
        self.track(self.admin,self.headers)
        owner,oh = self.login('owner'); self.track(owner,oh,tmdb_id=555)
        self.assertEqual(self.jobs(),[])

    def test_notification_is_part_of_request_transaction(self):
        self.save()
        with patch.object(self.mod,'enqueue_request_email',side_effect=RuntimeError('simulated storage failure')):
            self.assertEqual(self.alice.post('/api/tmdb/track',json=dict(media_type='tv',tmdb_id=123,season=1),headers=self.alice_headers).status_code,500)
        self.assertEqual(self.admin.get('/api/items').json['items'],[])
        self.assertEqual(self.jobs(),[])

    def test_successful_delivery_is_not_sent_again_and_survives_config_reads(self):
        self.save();self.track()
        with patch.object(self.mod,'send_notification') as send:
            self.assertTrue(self.mod.deliver_next_email())
            self.assertFalse(self.mod.deliver_next_email())
        send.assert_called_once()
        self.assertEqual(self.jobs()[0]['state'],'sent')
        self.assertEqual(send.call_args.args[1],'private-smtp-code')
        self.assertEqual(send.call_args.args[3],'owner@example.com')
        public = self.admin.get('/api/notifications').json
        self.assertEqual(public['pending_count'],0)
        self.assertTrue(public['last_sent_at'])
        self.assertEqual(public['last_error'],'')

    def test_send_failure_keeps_request_and_retries_with_backoff(self):
        self.save();self.track()
        with patch.object(self.mod,'send_notification',side_effect=mail.MailError('SMTP 登录失败')) as send:
            for attempt in range(1,5):
                self.assertTrue(self.mod.deliver_next_email())
                row = self.jobs()[0]
                self.assertEqual(row['attempts'],attempt)
                self.assertEqual(row['state'],'failed' if attempt==4 else 'retry')
                self.assertFalse(self.mod.deliver_next_email())
                with self.mod.database() as db:
                    db.execute('UPDATE email_outbox SET next_attempt=0')
        self.assertEqual(len(self.alice.get('/api/items').json['items']),1)
        response = self.admin.post('/api/notifications/retry',json={},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.jobs()[0]['attempts'],0)
        self.assertEqual(self.jobs()[0]['state'],'pending')

    def test_unexpected_errors_do_not_leak_credentials(self):
        self.save();self.track()
        with patch.object(self.mod,'send_notification',side_effect=RuntimeError('private-smtp-code fake-service-key')):
            self.mod.deliver_next_email()
            response = self.admin.post('/api/notifications/test',json={},headers=self.headers)
        self.assertEqual(response.status_code,502)
        self.assertNotIn('private-smtp-code',response.text + json.dumps(self.jobs()))

    def test_shutdown_or_recipient_change_cancels_unsent_jobs(self):
        self.save();self.track()
        self.admin.put('/api/notifications',json=dict(to_email='new-owner@example.com'),headers=self.headers)
        self.assertEqual(self.jobs()[0]['state'],'cancelled')
        self.track(season=2)
        self.assertEqual(next(row for row in self.jobs() if row['state']=='pending')['recipient'],'new-owner@example.com')
        self.admin.put('/api/notifications',json=dict(enabled=False),headers=self.headers)
        self.assertTrue(all(row['state']=='cancelled' for row in self.jobs()))
        with patch.object(self.mod,'send_notification') as send:
            self.assertFalse(self.mod.deliver_next_email())
        send.assert_not_called()

    def test_expired_delivery_lease_recovers_after_restart(self):
        self.save();self.track()
        with self.mod.database() as db:
            db.execute("UPDATE email_outbox SET state='sending',attempts=1,lease_until=1")
        with patch.object(self.mod,'send_notification') as send:
            self.assertTrue(self.mod.deliver_next_email())
        self.assertEqual(self.jobs()[0]['state'],'sent')
        self.assertEqual(self.jobs()[0]['attempts'],2)
        send.assert_called_once()

    def test_test_mail_allowed_before_enabling_without_historical_notifications(self):
        self.save(enabled=False)
        with patch.object(self.mod,'send_notification') as send:
            response=self.admin.post('/api/notifications/test',json={},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['ok'])
        self.assertEqual(response.json['last_sent_kind'],'test')
        self.assertEqual(send.call_args.args[2],{'test':True})
        self.assertEqual(self.jobs(),[])


class SMTPTests(unittest.TestCase):
    def setUp(self):
        self.config=dict(smtp_host='smtp.example.com',smtp_port=465,security='ssl',
            smtp_username='sender@example.com',smtp_password='encrypted',from_email='sender@example.com',
            to_email='owner@example.com',site_url='https://sy.example.com')
        self.payload=dict(title='<剧名>\r\nBcc:fake',kind='tv',season=2,username='<Alice>',year=2026,
            submitted_at='2026-10-25T10:00:00+08:00',tmdb={'id':123,'media_type':'tv'})

    def test_ssl_and_starttls_enforce_certificate_validation_before_login(self):
        for security in ('ssl','starttls'):
            self.config['security']=security
            with patch.object(mail.smtplib,'SMTP_SSL') as secure,patch.object(mail.smtplib,'SMTP') as plain:
                constructor=secure if security=='ssl' else plain
                client=constructor.return_value;client.send_message.return_value={}
                mail.send_notification(self.config,'smtp-secret',self.payload,'owner@example.com','unique-id')
                client.login.assert_called_once_with('sender@example.com','smtp-secret')
                client.send_message.assert_called_once()
                if security=='ssl':
                    plain.assert_not_called();context=secure.call_args.kwargs['context']
                else:
                    secure.assert_not_called();context=client.starttls.call_args.kwargs['context']
                    names=[entry[0] for entry in client.method_calls]
                    self.assertLess(names.index('starttls'),names.index('login'))
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode,mail.ssl.CERT_REQUIRED)

    def test_message_escapes_html_has_correct_season_and_no_credentials(self):
        message=mail.build_message(self.config,self.payload,'owner@example.com','unique-id')
        self.assertIn('第 2 季',message['Subject'])
        self.assertNotIn('\n',message['Subject'])
        markup=message.get_body(preferencelist=('html',)).get_content()
        self.assertIn('&lt;剧名&gt;',markup);self.assertIn('&lt;Alice&gt;',markup)
        self.assertIn('https://sy.example.com/#all',markup)
        self.assertIn('https://www.themoviedb.org/tv/123',markup)
        self.assertNotIn('smtp_password',message.as_string())
        movie=copy.deepcopy(self.payload);movie['kind']='movie'
        self.assertNotIn('第 2 季',mail.build_message(self.config,movie,'owner@example.com','id2')['Subject'])

    def test_auth_errors_are_sanitized(self):
        with patch.object(mail.smtplib,'SMTP_SSL') as smtp:
            smtp.return_value.login.side_effect=smtplib.SMTPAuthenticationError(535,b'private-smtp-code')
            with self.assertRaises(mail.MailError) as error:
                mail.send_notification(self.config,'private-smtp-code',self.payload,'owner@example.com','id')
        self.assertNotIn('private-smtp-code',str(error.exception))

    def test_quit_failure_does_not_retry_an_accepted_message(self):
        with patch.object(mail.smtplib,'SMTP_SSL') as smtp:
            smtp.return_value.send_message.return_value={}
            smtp.return_value.quit.side_effect=smtplib.SMTPResponseException(421,b'closing')
            mail.send_notification(self.config,'smtp-code',self.payload,'owner@example.com','id')
            smtp.return_value.send_message.assert_called_once()
            smtp.return_value.close.assert_called_once()


if __name__=='__main__':
    unittest.main()
