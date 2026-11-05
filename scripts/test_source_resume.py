import argparse
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import documents as docs
import transcribe
import anonymous_sources

URL = 'https://www.douyin.com/video/9876543210987654321'


def write_wav(path):
    with wave.open(str(path), 'wb') as f:
        f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        f.writeframes(b'\x10\0' * 32000)


class SourceResumeTests(unittest.TestCase):
    def run_case(self, root, old=None, fresh=None, failures=None, cached=False, overrides=False, convert_failure=False, real_metadata=False, config_extra=None, cookies=None):
        root = Path(root);job = root/'job';job.mkdir()
        source = {'id': '9876543210987654321', 'title': '源标题', 'platform': '抖音',
                  'source_url': URL, 'media_url': 'https://media.example/old.mp4', 'duration': 2}
        if old is not None:
            source.update(old);docs.write_json(job/'source.json', source)
        manifest = {'input': URL, 'limit_seconds': None, 'status': 'pending', 'created_at': '2026-11-03'}
        if cached:
            if cached == 'invalid':(job/'audio.wav').write_bytes(b'not a wav')
            else:write_wav(job/'audio.wav')
            manifest['audio_ready_sha256'] = docs.file_hash(job/'audio.wav')
        docs.write_json(job/'manifest.json', manifest)
        updated = {**source, 'media_url': 'https://media.example/new.mp4', **(fresh or {})}
        args = argparse.Namespace(input=URL, job=str(job), limit_seconds=None, output=None,
                                  cookies=cookies, source_url='https://source.example/original' if overrides else None,
                                  title='用户标题' if overrides else None, speakers='auto', speaker_evidence='')
        chunks = {'chunks': [{'start': 0, 'end': 2, 'text': '测试', 'tokens': ['测', '试'], 'timestamps': [.1, .5]}], 'elapsed_this_run': 0}
        self.metadata = (patch.object(transcribe.sources, 'metadata', wraps=transcribe.sources.metadata) if real_metadata
                         else patch.object(transcribe.sources, 'metadata', return_value=updated))
        self.acquire = patch.object(transcribe.sources, 'acquire', side_effect=failures or [ValueError('expired'), root/'media'])
        def convert(media, wav, limit):
            if convert_failure:raise ValueError('decode failure')
            write_wav(wav)
        with self.metadata as metadata, self.acquire as acquire, patch.object(transcribe.sources, 'convert', side_effect=convert), \
                patch.object(transcribe.engine, 'asr', return_value=chunks), \
                patch.object(transcribe.engine, 'diarize', return_value={'segments': [], 'report': {'count': 1, 'count_mode': 'estimated'}}):
            self.meta_mock = metadata;self.acquire_mock = acquire
            return transcribe.run(args, {'runtime': str(root), 'output': str(root/'out'), **(config_extra or {})})

    def test_native_download_denied_switches_to_api_after_one_refresh(self):
        native = {'id': '9876543210987654321', 'title': '源标题', 'platform': '抖音',
                  'source_url': URL, 'media_url': 'https://media.example/native.mp4',
                  'duration': 2, 'acquisition': 'douyin_public_app'}
        api = {**native, 'media_url': 'https://media.example/api.mp4',
               'acquisition': 'anonymous_api:example.com', 'uses_user_credentials': False}
        config = {'anonymous_api': {'enabled': True, 'platforms': ['抖音']}}
        with tempfile.TemporaryDirectory() as root, patch.object(anonymous_sources, 'douyin_metadata', return_value=native.copy()), \
                patch.object(anonymous_sources, 'api_metadata', return_value=api.copy()) as resolver:
            self.run_case(root, failures=[ValueError('403'), ValueError('403'), Path(root)/'media'],
                          real_metadata=True, config_extra=config)
            resolver.assert_called_once()
            self.assertEqual(self.acquire_mock.call_count, 3)
            manifest = docs.read_json(Path(root)/'job/manifest.json')
            source = docs.read_json(Path(root)/'job/source.json')
            self.assertEqual(source['acquisition'], 'anonymous_api:example.com')
            self.assertFalse(source['uses_user_credentials'])
            self.assertEqual(manifest['source_api_fallbacks'], 1)

    def test_download_fallback_does_not_repeat_an_already_anonymous_route(self):
        config = {'anonymous_api': {'enabled': True, 'platforms': ['抖音']}}
        with tempfile.TemporaryDirectory() as root, patch.object(anonymous_sources, 'api_metadata') as resolver:
            with self.assertRaises(ValueError):
                self.run_case(root, old={'acquisition': 'anonymous_api:example.com'},
                              fresh={'acquisition': 'douyin_public_app'},
                              failures=[ValueError('403'), ValueError('403')], config_extra=config)
            resolver.assert_not_called()

    def test_explicit_credentials_never_trigger_public_api_fallback(self):
        config = {'anonymous_api': {'enabled': True, 'platforms': ['抖音']}}
        with tempfile.TemporaryDirectory() as root, patch.object(anonymous_sources, 'api_metadata') as resolver:
            with self.assertRaises(ValueError):
                self.run_case(root, old={'acquisition': 'yt-dlp'}, fresh={'acquisition': 'yt-dlp'},
                              failures=[ValueError('403'), ValueError('403')], config_extra=config,
                              cookies='explicit-cookies.txt')
            resolver.assert_not_called()

    def test_download_fallback_failure_is_bounded_to_three_acquisitions(self):
        config = {'anonymous_api': {'enabled': True, 'platforms': ['抖音']}}
        fallback = {'id': '9876543210987654321', 'title': '源标题', 'platform': '抖音',
                    'source_url': URL, 'media_url': 'https://media.example/api.mp4',
                    'acquisition': 'anonymous_api:example.com', 'uses_user_credentials': False}
        with tempfile.TemporaryDirectory() as root, patch.object(anonymous_sources, 'api_metadata', return_value=fallback) as resolver:
            with self.assertRaises(ValueError):
                self.run_case(root, old={'acquisition': 'yt-dlp'}, fresh={'acquisition': 'yt-dlp'},
                              failures=[ValueError('403')]*3, config_extra=config)
            resolver.assert_called_once()
            self.assertEqual(self.acquire_mock.call_count, 3)
            self.assertEqual(len(docs.read_json(Path(root)/'job/manifest.json')['download_attempts']), 3)

    def test_download_fallback_rejects_changed_stable_identity(self):
        config = {'anonymous_api': {'enabled': True, 'platforms': ['抖音']}}
        fallback = {'id': '7699028070006623499', 'title': '源标题', 'platform': '抖音',
                    'source_url': URL, 'media_url': 'https://media.example/wrong.mp4',
                    'acquisition': 'anonymous_api:example.com', 'uses_user_credentials': False}
        with tempfile.TemporaryDirectory() as root, patch.object(anonymous_sources, 'api_metadata', return_value=fallback):
            with self.assertRaisesRegex(ValueError, 'ID'):
                self.run_case(root, old={'acquisition': 'yt-dlp'}, fresh={'acquisition': 'yt-dlp'},
                              failures=[ValueError('403')]*2, config_extra=config)
            self.assertEqual(self.acquire_mock.call_count, 2)

    def test_cached_expired_url_is_refreshed_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.run_case(root, old={})
            self.assertEqual(result['status'], 'review_pending')
            self.assertEqual(self.acquire_mock.call_count, 2)
            self.assertEqual(self.meta_mock.call_count, 1)
            self.assertIn('new.mp4', docs.read_json(Path(root)/'job/source.json')['media_url'])
            self.assertEqual(docs.read_json(Path(root)/'job/manifest.json')['source_refreshes'], 1)

    def test_fresh_url_failure_gets_only_one_refresh(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_case(root)
            self.assertEqual(self.meta_mock.call_count, 2)
            self.assertEqual(self.acquire_mock.call_count, 2)

    def test_second_failure_stops_instead_of_looping(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                self.run_case(root, old={}, failures=[ValueError('expired'), ValueError('still expired')])
            self.assertEqual(self.meta_mock.call_count, 1)
            self.assertEqual(self.acquire_mock.call_count, 2)

    def test_valid_wav_skips_source_network(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_case(root, old={}, cached=True)
            self.meta_mock.assert_not_called();self.acquire_mock.assert_not_called()

    def test_hash_matching_invalid_wav_does_not_skip_acquisition(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_case(root, old={}, cached='invalid', failures=[Path(root)/'media'])
            self.acquire_mock.assert_called_once();self.meta_mock.assert_not_called()

    def test_successful_acquisition_does_not_refresh_source(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_case(root, old={}, failures=[Path(root)/'media'])
            self.meta_mock.assert_not_called()
            self.assertNotIn('source_refreshes', docs.read_json(Path(root)/'job/manifest.json'))

    def test_conversion_failure_does_not_trigger_source_refresh(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, 'decode failure'):
                self.run_case(root, old={}, failures=[Path(root)/'media'], convert_failure=True)
            self.acquire_mock.assert_called_once();self.meta_mock.assert_not_called()

    def test_refresh_preserves_explicit_title_and_source_url(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_case(root, old={}, overrides=True)
            source = docs.read_json(Path(root)/'job/source.json')
            self.assertEqual(source['title'], '用户标题')
            self.assertEqual(source['source_url'], 'https://source.example/original')

    def test_refresh_rejects_different_work_or_input(self):
        for fresh in ({'id': '7699028070006623499'}, {'source_url': URL + 'different'}, {'platform': '小红书'}):
            with self.subTest(fresh=fresh), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(ValueError):self.run_case(root, old={}, fresh=fresh)
                self.assertEqual(self.acquire_mock.call_count, 1)

    def test_hash_id_change_requires_matching_title_and_keeps_stable_id(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_case(root, old={}, fresh={'id': '0123456789abcdef'})
            self.assertEqual(docs.read_json(Path(root)/'job/source.json')['id'], '9876543210987654321')
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                self.run_case(root, old={}, fresh={'id': '0123456789abcdef', 'title': '不同标题'})

    def test_local_source_failure_never_requeries_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            media = Path(root)/'input.wav';write_wav(media)
            with self.assertRaises(ValueError):self.run_case(root, old={'local_path': str(media)})
            self.meta_mock.assert_not_called()


if __name__ == '__main__':unittest.main()
