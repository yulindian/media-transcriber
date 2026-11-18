import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import video_download
import sources
import transcribe


class VideoDownloadTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.cfg={'runtime':str(self.root/'runtime'),'output':str(self.root/'输出 目录')}
        self.args=argparse.Namespace(input='https://example.com/demo.mp4',output=None,cookies=None)

    def test_download_publishes_video_and_never_transcribes(self):
        def acquire(info,job,cookies,*,video=False):
            self.assertTrue(video)
            file=job/'source.media';file.write_bytes(b'video fixture')
            return file
        with patch('sources.metadata',return_value={'title':'例子','id':'demo'}) as metadata, \
                patch('sources.acquire',side_effect=acquire), \
                patch('video_download.probe_video',return_value=('.mp4',3.0)), \
                patch('engine.asr') as asr, patch('sources.convert') as convert:
            result=transcribe.download_video(self.args,self.cfg)
            self.assertEqual(result['status'],'video_ready')
            self.assertEqual(Path(result['video_path']).read_bytes(),b'video fixture')
            self.assertEqual(Path(result['video_path']).suffix,'.mp4')
            metadata.assert_called_once_with(self.args.input,None,self.cfg,video=True)
            asr.assert_not_called();convert.assert_not_called()
            self.assertFalse(list(Path(self.cfg['output']).glob('*.md')))

    def test_audio_only_and_cover_art_are_rejected(self):
        for streams in ([{'codec_type':'audio'}],[{'codec_type':'video','disposition':{'attached_pic':1}}]):
            with self.subTest(streams=streams), patch('video_download.inspect_media',return_value={'streams':streams,'format':{'duration':'3','format_name':'mov,mp4,m4a'}}):
                with self.assertRaisesRegex(ValueError,'没有视频画面'):
                    video_download.probe_video(Path('audio.m4a'))

    def test_video_metadata_never_reuses_selected_audio_url(self):
        info={'id':'demo','url':'https://example.com/audio.m4a','protocol':'https'}
        with patch('sources.check_public_url'),patch('yt_dlp.YoutubeDL') as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value=info
            descriptor=sources._metadata_native(self.args.input,video=True)
        self.assertNotIn('media_url',descriptor)
        self.assertIn('vcodec!=none',ydl.call_args.args[0]['format'])

    def test_existing_different_file_is_preserved(self):
        source=self.root/'source.mp4';source.write_bytes(b'new')
        output=Path(self.cfg['output']);output.mkdir()
        existing=output/'example.mp4';existing.write_bytes(b'old')
        path=video_download.publish(source,output,'example','.mp4')
        self.assertEqual(existing.read_bytes(),b'old')
        self.assertEqual(path.read_bytes(),b'new')
        self.assertEqual(video_download.publish(source,output,'example','.mp4'),path)


if __name__=='__main__':unittest.main()
