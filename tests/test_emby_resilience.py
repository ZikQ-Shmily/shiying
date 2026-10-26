"""Transient library failures recover without retrying writes or exposing credentials."""
import json
import socket
import ssl
import unittest
from http.client import IncompleteRead, RemoteDisconnected
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

from integrations import EmbyClient, EmbyUserClient, EmbyAuthError, IntegrationError, fetch


class EmbyResilienceTests(unittest.TestCase):
    def response(self, value):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(value).encode()
        return response

    def client(self):
        return EmbyClient('http://private-host.example:9096/emby', 'private-test-token')

    def test_read_recovers_from_transient_failures_and_keeps_same_request(self):
        for error in (URLError(socket.timeout('unsafe private-host.example')), ConnectionResetError('private-test-token'),
                      RemoteDisconnected('private-test-token'), IncompleteRead(b'private-test-token', 50),
                      HTTPError('http://private-host.example?api_key=private-test-token', 504, 'upstream', {}, None)):
            with self.subTest(error=type(error).__name__):
                opener = Mock()
                opener.open.side_effect = [error, self.response({'Items': [], 'TotalRecordCount': 0})]
                with patch('integrations.build_opener', return_value=opener), patch('integrations.time.sleep') as sleep, \
                        self.assertLogs('integrations', level='INFO') as logs:
                    result = self.client().get('/Items', SearchTerm='private-title')
                self.assertEqual(result['Items'], [])
                self.assertEqual(opener.open.call_count, 2)
                sleep.assert_called_once_with(1)
                first, second = [call.args[0] for call in opener.open.call_args_list]
                self.assertEqual(first.full_url, second.full_url)
                self.assertEqual(first.get_header('X-emby-token'), 'private-test-token')
                for private in ('private-host', 'private-test-token', 'private-title', 'http://', '/Items'):
                    self.assertNotIn(private, '\n'.join(logs.output))
                self.assertIn('recovered', logs.output[-1])

    def test_final_timeout_is_bounded_specific_and_safe(self):
        opener = Mock()
        opener.open.side_effect = URLError(socket.timeout('http://private-host.example?token=private-test-token'))
        with patch('integrations.build_opener', return_value=opener), patch('integrations.time.sleep'), \
                self.assertLogs('integrations') as logs, self.assertRaises(IntegrationError) as caught:
            self.client().get('/Shows/private-id/Episodes', Season=1)
        self.assertEqual(opener.open.call_count, 2)
        self.assertIn('读取集数失败', str(caught.exception))
        self.assertIn('已尝试 2 次', str(caught.exception))
        self.assertIn('12 秒', str(caught.exception))
        self.assertIn('code=timeout', logs.output[-1])
        self.assertIn('elapsed_ms=', logs.output[-1])
        self.assertNotIn('private-', str(caught.exception) + '\n'.join(logs.output))

    def test_credentials_certificates_and_response_errors_are_not_retried(self):
        cases = [(HTTPError('http://private-host.example', status, 'unsafe', {}, None), EmbyAuthError)
                 for status in (401, 403)]
        cases += [(URLError(ssl.SSLCertVerificationError(1, 'unsafe certificate')), IntegrationError),
                  (URLError(socket.gaierror(socket.EAI_NONAME, 'unsafe domain')), IntegrationError),
                  (HTTPError('http://private-host.example', 429, 'unsafe', {}, None), IntegrationError)]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                opener = Mock()
                opener.open.side_effect = error
                with patch('integrations.build_opener', return_value=opener), patch('integrations.time.sleep') as sleep, \
                        self.assertLogs('integrations'), self.assertRaises(expected):
                    self.client().get('/Items')
                self.assertEqual(opener.open.call_count, 1)
                sleep.assert_not_called()
        opener = Mock()
        response = self.response({})
        response.read.return_value = b'<html>proxy unavailable</html>'
        opener.open.return_value = response
        with patch('integrations.build_opener', return_value=opener), self.assertLogs('integrations'), \
                self.assertRaises(IntegrationError):
            self.client().get('/Items')
        self.assertEqual(opener.open.call_count, 1)

    def test_login_logout_and_generic_post_are_never_replayed(self):
        for operation in (lambda: EmbyUserClient('http://emby.example').authenticate('user', 'password'),
                          lambda: EmbyUserClient('http://emby.example', 'token').logout(),
                          lambda: fetch('http://cms.example/run-once', body={'request_id': 'test'})):
            opener = Mock()
            opener.open.side_effect = URLError(socket.timeout('unsafe'))
            with patch('integrations.build_opener', return_value=opener), patch('integrations.time.sleep') as sleep, \
                    self.assertRaises(IntegrationError):
                operation()
            self.assertEqual(opener.open.call_count, 1)
            sleep.assert_not_called()

    def test_partial_comparison_does_not_turn_failure_into_absent_or_partial_success(self):
        opener = Mock()
        opener.open.side_effect = [self.response({'Items': [{'Id': 'series', 'Type': 'Series',
                                                             'ProviderIds': {'Tmdb': '123'}}]}),
                                   URLError(socket.timeout()), URLError(socket.timeout())]
        with patch('integrations.build_opener', return_value=opener), patch('integrations.time.sleep'), \
                self.assertLogs('integrations'), self.assertRaises(IntegrationError):
            self.client().compare('tv', 123, 1, ('Title',))
        self.assertEqual(opener.open.call_count, 3)


if __name__ == '__main__':
    unittest.main()
