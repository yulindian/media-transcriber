import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sources

BODY = b'\x00\x00\x00\x18ftypisom' + b'\0' * 4096
INFO = {'media_url': 'https://ynvan.com/api/proxy?url=target&sig=PRIVATE', 'transfer': 'curl'}


class CurlAcquisitionTests(unittest.TestCase):
    def setUp(self):
        resolver = patch('public_http.socket.getaddrinfo',side_effect=lambda host,*args,**kwargs:
                         [(None,None,None,None,('127.0.0.1' if host=='127.0.0.1' else '1.1.1.1',443))])
        resolver.start()
        self.addCleanup(resolver.stop)

    def transport(self, body=BODY, status=200, mime='video/mp4', reported=None, code=0):
        def run(command, **kwargs):
            self.command, self.kwargs = command, kwargs
            Path(command[command.index('--output') + 1]).write_bytes(body)
            return subprocess.CompletedProcess(command, code,
                f'{status}\n{mime}\n{len(body) if reported is None else reported}', 'secret signed stderr PRIVATE')
        return run

    def test_success_atomic_cache_and_no_network_on_cache_hit(self):
        with tempfile.TemporaryDirectory() as root, patch('sources.getproxies', return_value={}), \
                patch('sources.subprocess.run', side_effect=self.transport()) as run, \
                patch('sources.requests.get', side_effect=AssertionError('curl must handle transfer')):
            out = sources.acquire(INFO, root)
            self.assertEqual(out.read_bytes(), BODY)
            saved = json.loads(out.with_suffix(out.suffix + '.json').read_text())
            self.assertEqual(saved['sha256'], hashlib.sha256(BODY).hexdigest())
            self.assertEqual(saved['url_hash'], hashlib.sha256(INFO['media_url'].encode()).hexdigest())
            self.assertEqual(sources.acquire(INFO, root), out)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(self.command[-1], INFO['media_url'])
            self.assertEqual(self.command[1], '--disable')
            self.assertFalse(self.kwargs.get('shell', False))
            self.assertGreater(self.kwargs['timeout'], 0)
            self.assertLessEqual(self.kwargs['timeout'], 310)
            self.assertEqual(self.command[self.command.index('--proto-redir')+1], '=http,https')
            self.assertEqual(self.command[self.command.index('--proto')+1], '=http,https')
            self.assertEqual(self.command[self.command.index('--max-redirs')+1], '5')
            self.assertEqual(self.command[self.command.index('--max-time')+1], '300')
            self.assertEqual(self.command[self.command.index('--connect-timeout')+1], '15')
            self.assertFalse(list(Path(root).glob('*.part')))

    def test_system_proxy_is_forwarded_without_hardcoding(self):
        with tempfile.TemporaryDirectory() as root, patch('sources.getproxies', return_value={'https': 'http://proxy.example:8888'}), \
                patch('sources.subprocess.run', side_effect=self.transport()):
            sources.acquire(INFO, root)
            self.assertEqual(self.command[self.command.index('--proxy')+1], 'http://proxy.example:8888')

    def test_redirect_to_loopback_is_never_fetched(self):
        def redirect(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, '302\ntext/html\n0\nhttp://127.0.0.1/private', '')
        with tempfile.TemporaryDirectory() as root, patch('sources.subprocess.run', side_effect=redirect) as run:
            with self.assertRaisesRegex(ValueError,'本地或私有'):
                sources.acquire(INFO,root)
            run.assert_called_once()
            self.assertNotIn('--location',run.call_args.args[0])

    def test_bad_response_cleans_part_and_never_publishes(self):
        cases = [dict(status=206), dict(body=b''), dict(reported=len(BODY)+1),
                 dict(mime='image/png'), dict(mime='text/html'), dict(mime='application/json'),
                 dict(mime='application/xml'), dict(mime='application/problem+json'),
                 dict(body=b'<html>login</html>'), dict(body=b'{"error":1}'), dict(code=22)]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root, \
                    patch('sources.getproxies', return_value={}), patch('sources.subprocess.run', side_effect=self.transport(**case)):
                with self.assertRaises(ValueError) as raised:sources.acquire(INFO, root)
                self.assertNotIn('PRIVATE', str(raised.exception))
                self.assertEqual(list(Path(root).iterdir()), [])

    def test_timeout_error_does_not_leak_command_or_signature(self):
        with tempfile.TemporaryDirectory() as root, patch('sources.getproxies', return_value={}), \
                patch('sources.subprocess.run', side_effect=subprocess.TimeoutExpired(['curl', INFO['media_url']], 310)):
            with self.assertRaises(ValueError) as raised:sources.acquire(INFO, root)
            self.assertNotIn('PRIVATE', str(raised.exception))
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_malformed_status_is_failure_and_cleans_written_media(self):
        def malformed(command, **kwargs):
            Path(command[command.index('--output')+1]).write_bytes(BODY)
            return subprocess.CompletedProcess(command, 0, 'not a transfer status', '')
        with tempfile.TemporaryDirectory() as root, patch('sources.getproxies', return_value={}), \
                patch('sources.subprocess.run', side_effect=malformed):
            with self.assertRaises(ValueError):sources.acquire(INFO, root)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_missing_curl_is_controlled_failure(self):
        with tempfile.TemporaryDirectory() as root, patch('sources.getproxies', return_value={}), \
                patch('sources.subprocess.run', side_effect=FileNotFoundError('curl')):
            with self.assertRaises(ValueError):sources.acquire(INFO, root)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_damaged_cached_media_is_redownloaded(self):
        with tempfile.TemporaryDirectory() as root, patch('sources.getproxies', return_value={}), \
                patch('sources.subprocess.run', side_effect=self.transport()) as run:
            out = sources.acquire(INFO, root);out.write_bytes(b'broken')
            self.assertEqual(sources.acquire(INFO, root).read_bytes(), BODY)
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':unittest.main()
