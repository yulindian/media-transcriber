"""Offline fault-injection checks for source acquisition; no account data required."""
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.parse import quote

import sources
import requests


BODY = b'ID3' + b'\x00' * 4096


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).calls += 1
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/private')
            self.end_headers()
            return
        body = b'<html>login required</html>' if self.path.startswith('/html') else BODY
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')  # even fake MIME must be rejected
        self.send_header('Content-Length', str(len(body) + (100 if self.path.startswith('/cut') else 0)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


class SourceRobustnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_public_download_blocks_loopback_and_configured_resolver_cannot_redirect_to_it(self):
        with tempfile.TemporaryDirectory() as root:
            calls=Handler.calls
            with self.assertRaisesRegex(ValueError,'本地或私有'):
                sources.acquire({'media_url':self.base+'/ok.mp3'},root)
            self.assertEqual(Handler.calls,calls)
            with self.assertRaisesRegex(ValueError,'本地或私有'):
                sources.acquire({'media_url':self.base+'/redirect','acquisition':'configured_resolver'},root)
            self.assertEqual(Handler.calls,calls+1)

    def test_interrupted_download_is_not_published_and_partial_is_cleaned(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                sources.acquire({'acquisition': 'configured_resolver', 'media_url': self.base + '/cut.mp3?token=SECRET'}, root)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_html_disguised_as_audio_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, '音视频|media'):
                sources.acquire({'acquisition': 'configured_resolver', 'media_url': self.base + '/html.mp3'}, root)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_length_mismatch_even_if_transport_does_not_raise(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {'Content-Type': 'audio/mpeg', 'Content-Length': str(len(BODY) + 1)}
        response.iter_content.return_value = [BODY]
        with tempfile.TemporaryDirectory() as root, patch('sources.public_get', return_value=response):
            with self.assertRaisesRegex(ValueError, '长度'):
                sources.acquire({'acquisition': 'configured_resolver', 'media_url': self.base + '/ok.mp3'}, root)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_network_timeout_is_bounded_sanitized_and_cleans_partial(self):
        with tempfile.TemporaryDirectory() as root, patch('sources.public_get', side_effect=requests.Timeout('https://host/media?secret=PRIVATE')) as get:
            with self.assertRaises(ValueError) as raised:
                sources.acquire({'acquisition': 'configured_resolver', 'media_url': self.base + '/ok.mp3'}, root)
            self.assertNotIn('PRIVATE', str(raised.exception))
            get.assert_called_once()
            self.assertEqual(get.call_args.kwargs['timeout'], (25, 90))
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_truncated_cache_is_redownloaded_and_valid_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as root:
            info = {'acquisition': 'configured_resolver', 'media_url': self.base + '/ok.mp3'}
            out = sources.acquire(info, root)
            out.write_bytes(BODY[:100])
            calls = Handler.calls
            self.assertEqual(sources.acquire(info, root).read_bytes(), BODY)
            self.assertEqual(Handler.calls, calls + 1)
            sources.acquire(info, root)
            self.assertEqual(Handler.calls, calls + 1)

    def test_unverified_legacy_cache_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'source.mp3').write_bytes(b'ID3short')
            self.assertEqual(sources.acquire({'acquisition': 'configured_resolver', 'media_url': self.base + '/ok.mp3'}, root).read_bytes(), BODY)

    def test_same_length_cache_corruption_and_changed_url_are_not_reused(self):
        with tempfile.TemporaryDirectory() as root:
            info = {'acquisition': 'configured_resolver', 'media_url': self.base + '/ok.mp3'}
            out = sources.acquire(info, root)
            out.write_bytes(b'BAD' + BODY[3:])
            calls = Handler.calls
            self.assertEqual(sources.acquire(info, root).read_bytes(), BODY)
            sources.acquire({'acquisition': 'configured_resolver', 'media_url': self.base + '/other.mp3'}, root)
            self.assertEqual(Handler.calls, calls + 2)

    def test_xhs_normalization_preserves_signed_query_exactly(self):
        query = 'xsec_token=a%2Fb%2Bc+z&signature=SIGNED%2Fv&xsec_source=pc&other=1&other=2'
        result = sources.normalize_xhs('https://www.xiaohongshu.com/discovery/item/abcdef?' + query)
        self.assertEqual(result, 'https://www.xiaohongshu.com/explore/abcdef?' + query)

    def test_untrusted_redirect_path_is_not_followed(self):
        url = 'https://example.org/?redirectPath=' + quote('https://www.xiaohongshu.com/explore/abcdef')
        self.assertEqual(sources.normalize_xhs(url), url)

    def test_shortlink_redirect_cannot_escape_xhs(self):
        response = MagicMock(url='https://example.org/?token=SECRET')
        with patch('sources.public_get', return_value=response), patch('yt_dlp.YoutubeDL') as downloader:
            downloader.return_value.__enter__.return_value.extract_info.return_value = {'id': 'wrong'}
            with self.assertRaisesRegex(ValueError, '小红书'):
                sources.metadata('https://xhslink.com/abc')

    def test_resolver_timeout_and_failure_are_sanitized(self):
        config = {'resolvers': {'example.org': ['resolver', '{url}']}}
        url = 'https://example.org/video?token=SECRET'
        failures = [subprocess.TimeoutExpired(['resolver', url], 90),
                    subprocess.CalledProcessError(1, ['resolver', url], stderr='Cookie: PRIVATE\ntoken error')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch('sources.subprocess.run', side_effect=failure):
                with self.assertRaises(ValueError) as raised:
                    sources.metadata(url, config=config)
                self.assertNotIn('SECRET', str(raised.exception))
                self.assertNotIn('PRIVATE', str(raised.exception))

    def test_resolver_invalid_output_is_not_success(self):
        config = {'resolvers': {'example.org': ['resolver']}}
        for output in ['not JSON', '[]', '{"media_url":"file:///private"}']:
            with self.subTest(output=output), patch('sources.subprocess.run', return_value=MagicMock(stdout=output)):
                with self.assertRaises(ValueError):
                    sources.metadata('https://example.org', config=config)

    def test_cookie_auth_failure_is_not_success(self):
        with patch('yt_dlp.YoutubeDL') as downloader:
            downloader.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError('login required Cookie: PRIVATE')
            with self.assertRaises(ValueError) as raised:
                sources.metadata('https://www.youtube.com/watch?v=abc', cookies='authorized.txt')
            self.assertNotIn('PRIVATE', str(raised.exception))

    def test_cookie_metadata_without_playable_media_is_not_success(self):
        with patch('yt_dlp.YoutubeDL') as downloader:
            downloader.return_value.__enter__.return_value.extract_info.return_value = {'id': 'login', 'formats': []}
            with self.assertRaises(ValueError):
                sources.metadata('https://www.youtube.com/watch?v=abc', cookies='authorized.txt')

    def test_cookie_download_failure_is_not_success_or_secret_leak(self):
        with tempfile.TemporaryDirectory() as root, patch('yt_dlp.YoutubeDL') as downloader:
            downloader.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError('login required https://host/?token=PRIVATE Cookie: SECRET')
            with self.assertRaises(ValueError) as raised:
                sources.acquire({'resolved_url': 'https://www.youtube.com/watch?v=abc'}, root, cookies='authorized.txt')
            self.assertNotIn('PRIVATE', str(raised.exception))
            self.assertNotIn('SECRET', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
