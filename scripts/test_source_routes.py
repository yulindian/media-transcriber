"""Route selection and resume invariants, independent of live services."""
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests
import sources

CONFIG = {'anonymous_api': {'enabled': True, 'platforms': ['视频号', 'YouTube', '抖音']}}


class RouteTests(unittest.TestCase):
    @patch('yt_dlp.YoutubeDL')
    def test_native_selected_http_audio_reuses_extraction_and_filters_headers(self,downloader):
        downloader.return_value.__enter__.return_value.extract_info.return_value={
            'id':'target','title':'Target','url':'https://cdn.test/audio.m4a','protocol':'https',
            'http_headers':{'Referer':'https://www.bilibili.com/','Cookie':'PRIVATE','Authorization':'PRIVATE'}}
        result=sources._metadata_native('https://www.bilibili.com/video/BVtarget')
        self.assertEqual(result['media_url'],'https://cdn.test/audio.m4a')
        self.assertEqual(result['media_headers'],{'Referer':'https://www.bilibili.com/'})
        options=downloader.call_args.args[0]
        self.assertEqual(options['format'],'bestaudio/best')

    @patch('anonymous_sources.api_metadata')
    @patch('sources._metadata_native')
    def test_channels_uses_enabled_share_parser_without_unsupported_downloader(self, native, api):
        api.return_value = {'id': 'AN7', 'media_url': 'https://media.test/v.mp4'}
        self.assertEqual(sources.metadata('https://weixin.qq.com/sph/AN7', config=CONFIG)['id'], 'AN7')
        api.assert_called_once(); native.assert_not_called()

    @patch('anonymous_sources.api_metadata')
    @patch('sources._metadata_native', side_effect=ValueError('blocked'))
    def test_youtube_native_failure_uses_one_anonymous_fallback(self, native, api):
        api.return_value = {'id': 'video', 'media_url': 'https://media.test/v.mp4'}
        self.assertEqual(sources.metadata('https://www.youtube.com/watch?v=video', config=CONFIG)['id'], 'video')
        api.assert_called_once()

    @patch('anonymous_sources.api_metadata')
    @patch('sources._metadata_native', side_effect=ValueError('blocked'))
    def test_configured_credentials_are_not_forwarded_to_public_service(self, native, api):
        with self.assertRaises(ValueError):
            sources.metadata('https://www.youtube.com/watch?v=video', cookies='private.txt', config=CONFIG)
        api.assert_not_called()

    def test_image_response_is_rejected_before_publishing_media(self):
        response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        response.headers={'Content-Type':'image/jpeg'}
        with tempfile.TemporaryDirectory() as root, patch('sources.public_get',return_value=response):
            with self.assertRaisesRegex(ValueError,'媒体|音视频'):
                sources.acquire({'media_url':'https://media.test/cover.jpg'},root)


if __name__ == '__main__':
    unittest.main()
