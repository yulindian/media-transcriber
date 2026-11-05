import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import engine


class AsrLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.job = Path(self.temp.name)/'job';self.job.mkdir()
        self.models = Path(self.temp.name)/'models'
        model_dir = self.models/engine.SENSE;model_dir.mkdir(parents=True)
        (model_dir/'model.int8.onnx').write_bytes(b'model')
        (model_dir/'tokens.txt').write_text('tokens')
        with wave.open(str(self.job/'audio.wav'), 'wb') as f:
            f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            f.writeframes(b'\x10\0'*48000)
        self.legacy = engine.signature(self.job, 'asr-v1', {
            'model': engine.file_hash(model_dir/'model.int8.onnx'),
            'tokens': engine.file_hash(model_dir/'tokens.txt')})

    def recognizer(self):
        stream = SimpleNamespace(result=SimpleNamespace(tokens=['hello'], timestamps=[.1], text='hello'),
                                 accept_waveform=lambda *args: None)
        return SimpleNamespace(create_stream=lambda: stream, decode_stream=lambda *args: None)

    def test_supported_languages_reach_recognizer_and_have_isolated_signatures(self):
        signatures = []
        with patch.object(engine.sherpa_onnx.OfflineRecognizer, 'from_sense_voice', return_value=self.recognizer()) as factory, \
                patch.object(engine.subprocess, 'run', return_value=SimpleNamespace(stderr='')):
            for language in ('zh', 'auto', 'en', 'yue', 'ja', 'ko'):
                result = engine.asr(self.job, self.models, language=language)
                self.assertEqual(factory.call_args.kwargs['language'], language)
                signatures.append(result['signature'])
        self.assertEqual(len(set(signatures)), 6)
        self.assertEqual(signatures[0], self.legacy)

    def test_default_zh_reuses_legacy_cache_without_model_loading(self):
        cached = {'signature': self.legacy, 'duration': 3,
                  'chunks': [{'start': 0, 'end': 3, 'text': '旧', 'tokens': ['旧'], 'timestamps': [.1]}]}
        engine.write_json(self.job/'asr.json', cached)
        with patch.object(engine.sherpa_onnx.OfflineRecognizer, 'from_sense_voice') as factory, \
                patch.object(engine, 'audio', side_effect=AssertionError('cache should not decode')):
            self.assertEqual(engine.asr(self.job, self.models), cached)
            factory.assert_not_called()

    def test_auto_does_not_reuse_legacy_chinese_cache_or_checkpoint(self):
        cached = {'signature': self.legacy, 'duration': 3,
                  'chunks': [{'start': 0, 'end': 3, 'text': '旧', 'tokens': ['旧'], 'timestamps': [.1]}]}
        engine.write_json(self.job/'asr.json', cached)
        engine.write_json(self.job/'asr.signature.json', {'signature': self.legacy})
        import json
        (self.job/'asr.chunks.jsonl').write_text(json.dumps(cached['chunks'][0])+'\n')
        with patch.object(engine.sherpa_onnx.OfflineRecognizer, 'from_sense_voice', return_value=self.recognizer()) as factory, \
                patch.object(engine.subprocess, 'run', return_value=SimpleNamespace(stderr='')):
            result = engine.asr(self.job, self.models, language='auto')
            factory.assert_called_once()
        self.assertNotEqual(result['signature'], self.legacy)
        self.assertEqual(result['chunks'][0]['text'], 'hello')

    def test_invalid_languages_fail_before_model_or_file_loading(self):
        with patch.object(engine.sherpa_onnx.OfflineRecognizer, 'from_sense_voice') as factory, \
                patch.object(engine, 'file_hash') as file_hash:
            for language in ('de', '', None, True, []):
                with self.subTest(language=language), self.assertRaises(ValueError):
                    engine.asr(self.job, self.models, language=language)
            factory.assert_not_called();file_hash.assert_not_called()


if __name__ == '__main__':unittest.main()
