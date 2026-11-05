"""Public transfers never inherit account credentials or share a session."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import public_http


def response(url='https://public.example/media'):
    result = requests.Response()
    result.status_code = 200
    result.url = url
    result._content = b'{"ok":true}'
    return result


class PublicHTTPTests(unittest.TestCase):
    def test_requests_effective_url_must_also_be_public_and_uncredentialed(self):
        for target in ('https://account:password@public.example/final', 'file:///private.txt'):
            session = Mock()
            session.get.return_value = response(target)
            with self.subTest(target=target), patch('public_http.requests.Session', return_value=session), \
                    patch('public_http.getproxies', return_value={}), \
                    patch('public_http.proxy_bypass', return_value=False), self.assertRaises(ValueError):
                public_http.get('https://public.example/start')
            session.close.assert_called_once()

    def fallback_transfer(self, body=b'{"ok":true}', status='200',
                          effective='https://public.example/final', content_type='application/json', code=0):
        paths = []

        def run(command, **kwargs):
            target = Path(command[command.index('--output') + 1])
            paths.append(target)
            target.write_bytes(body)
            return Mock(returncode=code, stdout=f'{status}\n{content_type}\n{effective}',
                        stderr='private diagnostic signature=must-not-be-exposed')
        return paths, run

    def test_opted_in_non_stream_network_failure_uses_curl_response(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.SSLError('TLS EOF')
        paths, transfer = self.fallback_transfer(content_type='application/json; charset=utf-8')
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={}), \
                patch('public_http.proxy_bypass', return_value=False), \
                patch('subprocess.run', side_effect=transfer) as run:
            result = public_http.get('https://public.example/start', allow_curl_fallback=True)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.url, 'https://public.example/final')
        self.assertEqual(result.json(), {'ok': True})
        result.raise_for_status()
        session.close.assert_called_once()
        run.assert_called_once()
        self.assertNotIn('allow_curl_fallback', session.get.call_args.kwargs)
        self.assertTrue(paths and all(not path.exists() for path in paths))

    def test_curl_does_not_run_by_default_or_for_streams(self):
        for options in ({}, {'allow_curl_fallback': True, 'stream': True}):
            session = Mock()
            session.get.side_effect = requests.ConnectionError('offline')
            with self.subTest(options=options), \
                    patch('public_http.requests.Session', return_value=session), \
                    patch('public_http.getproxies', return_value={}), \
                    patch('public_http.proxy_bypass', return_value=False), \
                    patch('subprocess.run') as run, self.assertRaises(requests.RequestException):
                public_http.get('https://public.example/start', **options)
            run.assert_not_called()
            session.close.assert_called_once()

    def test_curl_uses_bounded_anonymous_arguments_and_header_allowlist(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError('offline')
        paths, transfer = self.fallback_transfer()
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={'https': 'http://127.0.0.1:7897'}), \
                patch('public_http.proxy_bypass', return_value=False), \
                patch('subprocess.run', side_effect=transfer) as run:
            public_http.get('https://public.example/parse', params={'url':'https://video.example/watch?v=abc'},
                            headers={'User-Agent':'test-agent', 'Accept':'application/json',
                                     'X-Requested-With':'XMLHttpRequest', 'X-Unrelated-Secret':'not-to-forward'},
                            allow_curl_fallback=True)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['curl', '--disable'])
        self.assertIn('--no-netrc', command)
        self.assertEqual(command[command.index('--proto')+1], '=http,https')
        self.assertEqual(command[command.index('--proto-redir')+1], '=http,https')
        self.assertLessEqual(int(command[command.index('--max-redirs')+1]), 5)
        self.assertLessEqual(float(command[command.index('--max-time')+1]), 60)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 65)
        self.assertEqual(command[command.index('--proxy')+1], 'http://127.0.0.1:7897')
        self.assertIn('User-Agent: test-agent', command)
        self.assertIn('Accept: application/json', command)
        self.assertNotIn('not-to-forward', str(command))
        self.assertTrue(command[-1].startswith('https://public.example/parse?url='))
        self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_curl_respects_localhost_bypass_even_when_proxy_environment_exists(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError('offline')
        paths, transfer = self.fallback_transfer(effective='http://localhost:2022/status')
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={'http':'http://127.0.0.1:7897'}), \
                patch('public_http.proxy_bypass', return_value=False), \
                patch('subprocess.run', side_effect=transfer) as run:
            public_http.get('http://localhost:2022/status', allow_curl_fallback=True)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--proxy')+1], '')

    def test_curl_rejects_credentialed_or_non_http_effective_url_and_cleans_up(self):
        for target in ('https://user:password@public.example/final','file:///private.txt'):
            session = Mock()
            session.get.side_effect = requests.ConnectionError('offline')
            paths, transfer = self.fallback_transfer(effective=target)
            with self.subTest(target=target), patch('public_http.requests.Session', return_value=session), \
                    patch('public_http.getproxies', return_value={}), \
                    patch('public_http.proxy_bypass', return_value=False), \
                    patch('subprocess.run', side_effect=transfer), self.assertRaises(ValueError):
                public_http.get('https://public.example/start', allow_curl_fallback=True)
            self.assertTrue(paths and all(not path.exists() for path in paths))

    def test_curl_failure_is_sanitized_and_temporary_body_is_removed(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError('offline')
        paths, transfer = self.fallback_transfer(code=35)
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={}), \
                patch('public_http.proxy_bypass', return_value=False), \
                patch('subprocess.run', side_effect=transfer), self.assertRaises(ValueError) as raised:
            public_http.get('https://public.example/start?sig=private-input-signature', allow_curl_fallback=True)
        self.assertNotIn('private', str(raised.exception))
        self.assertTrue(paths and all(not path.exists() for path in paths))

    def test_curl_subprocess_timeout_does_not_echo_signed_command(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError('offline')
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 60, stderr='private stderr')
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={}), \
                patch('public_http.proxy_bypass', return_value=False), \
                patch('subprocess.run', side_effect=timeout), self.assertRaises(ValueError) as raised:
            public_http.get('https://public.example/start?sig=private-input-signature', allow_curl_fallback=True)
        self.assertNotIn('private', str(raised.exception))

    def test_curl_rejects_oversized_non_stream_body(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError('offline')
        paths = []
        def transfer(command, **kwargs):
            target = Path(command[command.index('--output')+1]);paths.append(target)
            with target.open('wb') as stream:
                stream.truncate(8*1024*1024+1)
            return Mock(returncode=0,stdout='200\napplication/json\nhttps://public.example/final')
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={}), \
                patch('public_http.proxy_bypass', return_value=False), \
                patch('subprocess.run', side_effect=transfer), self.assertRaises(ValueError):
            public_http.get('https://public.example/start', allow_curl_fallback=True)
        self.assertTrue(paths and all(not path.exists() for path in paths))

    def test_curl_http_auth_failure_remains_http_failure(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError('offline')
        paths, transfer = self.fallback_transfer(status='401')
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={}), \
                patch('public_http.proxy_bypass', return_value=False), \
                patch('subprocess.run', side_effect=transfer):
            result = public_http.get('https://public.example/start', allow_curl_fallback=True)
        with self.assertRaises(requests.HTTPError):
            result.raise_for_status()

    def test_fake_personal_netrc_is_not_read_or_transmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            netrc = Path(directory) / 'netrc'
            netrc.write_text('machine public.example login private_test_account password private_test_password\n', encoding='utf-8')
            prepared = []

            def send(session, request, **kwargs):
                prepared.append(request)
                return response(request.url)

            with patch.dict(os.environ, {'NETRC': str(netrc)}), \
                    patch('public_http.getproxies', return_value={}), \
                    patch('requests.sessions.Session.send', send):
                result = public_http.get('https://public.example/media')
            self.assertEqual(result.json(), {'ok': True})
            self.assertEqual(len(prepared), 1)
            self.assertNotIn('Authorization', prepared[0].headers)
            self.assertNotIn('Cookie', prepared[0].headers)

    def test_sessions_and_cookie_jars_are_not_shared_between_calls(self):
        sessions = []
        cookies = []

        def send(session, request, **kwargs):
            sessions.append(session)
            cookies.append(request.headers.get('Cookie'))
            session.cookies.set('anonymous_session', 'first_response_value')
            return response(request.url)

        with patch('public_http.getproxies', return_value={}), \
                patch('requests.sessions.Session.send', send):
            public_http.get('https://public.example/one')
            public_http.get('https://public.example/two')
        self.assertIsNot(sessions[0], sessions[1])
        self.assertEqual(cookies, [None, None])
        self.assertTrue(all(session.trust_env is False for session in sessions))

    def test_network_proxy_is_inherited_without_mutating_system_mapping(self):
        mapping = {'https': 'http://127.0.0.1:7897', 'no': 'localhost,127.0.0.1'}
        observed = []

        def send(session, request, **kwargs):
            observed.append(kwargs.get('proxies'))
            return response(request.url)

        with patch('public_http.getproxies', return_value=mapping), \
                patch('public_http.proxy_bypass', return_value=False), \
                patch('requests.sessions.Session.send', send):
            public_http.get('https://public.example/media')
        self.assertEqual(observed, [{'https': 'http://127.0.0.1:7897'}])
        self.assertEqual(mapping, {'https': 'http://127.0.0.1:7897', 'no': 'localhost,127.0.0.1'})

    def test_localhost_and_loopback_never_go_through_network_proxy(self):
        observed = []

        def send(session, request, **kwargs):
            observed.append(kwargs.get('proxies'))
            return response(request.url)

        with patch('public_http.getproxies', return_value={'http': 'http://127.0.0.1:7897'}), \
                patch('public_http.proxy_bypass', return_value=False), \
                patch('requests.sessions.Session.send', send):
            for url in ('http://localhost:2022/status', 'http://127.0.0.1:2022/status', 'http://[::1]:2022/status'):
                public_http.get(url)
        self.assertEqual(observed, [{}, {}, {}])

    def test_system_proxy_bypass_is_respected(self):
        observed = []

        def send(session, request, **kwargs):
            observed.append(kwargs.get('proxies'))
            return response(request.url)

        with patch('public_http.getproxies', return_value={'http': 'http://127.0.0.1:7897'}), \
                patch('public_http.proxy_bypass', return_value=True), \
                patch('requests.sessions.Session.send', send):
            public_http.get('http://intranet.example/status')
        self.assertEqual(observed, [{}])

    def test_non_stream_response_is_buffered_and_session_is_closed(self):
        session = Mock()
        result = response()
        session.get.return_value = result
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={}), \
                patch('public_http.proxy_bypass', return_value=False):
            returned = public_http.get('https://public.example/media')
        session.close.assert_called_once()
        self.assertEqual(returned.json(), {'ok': True})

    def test_stream_closes_session_only_when_response_is_closed(self):
        session = Mock()
        result = response()
        result.close = Mock()
        original_close = result.close
        session.get.return_value = result
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={}), \
                patch('public_http.proxy_bypass', return_value=False):
            returned = public_http.get('https://public.example/media', stream=True)
        session.close.assert_not_called()
        returned.close()
        returned.close()
        original_close.assert_called_once()
        session.close.assert_called_once()

    def test_request_failure_also_closes_session(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError('offline')
        with patch('public_http.requests.Session', return_value=session), \
                patch('public_http.getproxies', return_value={}), \
                patch('public_http.proxy_bypass', return_value=False), \
                self.assertRaises(requests.ConnectionError):
            public_http.get('https://public.example/media')
        session.close.assert_called_once()

    def test_explicit_account_credentials_are_rejected_before_network(self):
        with patch('public_http.requests.Session') as session:
            for options in ({'auth': ('account', 'secret')}, {'cookies': {'session': 'secret'}},
                            {'headers': {'Cookie': 'session=secret'}},
                            {'headers': {'Authorization': 'Bearer secret'}}):
                with self.subTest(options=list(options)), self.assertRaises(ValueError):
                    public_http.get('https://public.example/media', **options)
        session.assert_not_called()


if __name__ == '__main__':
    unittest.main()
