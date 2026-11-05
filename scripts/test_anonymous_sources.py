"""Anonymous acquisition contracts; every HTTP call is mocked."""
import unittest
from unittest.mock import Mock, patch

import requests
import anonymous_sources as anonymous


VID = '9876543210987654321'
DOUYIN = 'https://www.douyin.com/video/' + VID
CHANNELS = 'https://weixin.qq.com/sph/ExampleShareId'
MEDIA = 'https://finder.video.qq.com/video.mp4?token=ephemeral'


def response(payload, url=None):
    result = Mock()
    result.status_code = 200
    result.json.return_value = payload
    result.raise_for_status.return_value = None
    result.url = url or DOUYIN
    return result


def config(**updates):
    options = {'enabled': True, 'endpoint': 'https://api.bugpk.com/api/short_videos',
               'timeout': 25, 'platforms': ['视频号', 'YouTube', '抖音']}
    options.update(updates)
    return {'anonymous_api': options}


def video(**updates):
    data = {'type': 'video', 'title': '公共样本', 'url': MEDIA,
            'author': {'name': '公开作者'}}
    data.update(updates)
    return {'code': 200, 'data': data}


class AnonymousSourcesTests(unittest.TestCase):
    @patch('anonymous_sources.public_get')
    def test_external_explicit_native_identity_must_match_input(self, get):
        for field in ('id', 'video_id', 'aweme_id'):
            get.return_value = response(video(**{field: 'a_different_video'}))
            with self.subTest(field=field), self.assertRaises(ValueError):
                anonymous.api_metadata('https://www.youtube.com/watch?v=TestVideo01', 'YouTube', config())

    @patch('anonymous_sources.public_get')
    def test_external_matching_native_identity_is_accepted(self, get):
        get.return_value = response(video(video_id='TestVideo01'))
        result = anonymous.api_metadata('https://www.youtube.com/watch?v=TestVideo01', 'YouTube', config())
        self.assertEqual(result['id'], 'TestVideo01')

    @patch('anonymous_sources.public_get')
    def test_douyin_rejects_unknown_or_credentialed_input_before_network(self, get):
        for url in ('https://evil.example/video/' + VID,
                    'https://douyin.com.evil.example/video/' + VID,
                    'https://user:password@www.douyin.com/video/' + VID):
            with self.subTest(url=url), self.assertRaises(ValueError):
                anonymous.douyin_metadata(url)
        get.assert_not_called()

    @patch('anonymous_sources.public_get')
    def test_douyin_rejects_foreign_short_link_redirect(self, get):
        get.return_value = response({}, 'https://evil.example/video/' + VID)
        with self.assertRaises(ValueError):
            anonymous.douyin_metadata('https://v.douyin.com/example/')
        self.assertEqual(get.call_count, 1)

    @patch('anonymous_sources.public_get')
    def test_douyin_rejects_credentialed_short_link_redirect(self, get):
        get.return_value = response({}, 'https://user:password@www.douyin.com/video/' + VID)
        with self.assertRaises(ValueError):
            anonymous.douyin_metadata('https://v.douyin.com/example/')
        self.assertEqual(get.call_count, 1)

    @patch('anonymous_sources.public_get')
    def test_douyin_matches_target_instead_of_first_recommendation(self, get):
        def item(identity, title):
            return {'aweme_id': identity, 'desc': title, 'author': {'nickname': '作者'},
                    'video': {'duration': 82269, 'play_addr': {'url_list': [MEDIA]}}}
        get.return_value = response({'status_code': 0, 'aweme_list': [
            item('1234567890123456789', '推荐作品'), item(VID, '目标作品')]})
        result = anonymous.douyin_metadata(DOUYIN)
        self.assertEqual(result['id'], VID)
        self.assertEqual(result['title'], '目标作品')
        self.assertAlmostEqual(result['duration'], 82.269)
        self.assertEqual(result['source_url'], DOUYIN)
        self.assertEqual(result['media_url'], MEDIA)

    @patch('anonymous_sources.public_get')
    def test_douyin_missing_target_is_failure(self, get):
        get.return_value = response({'status_code': 0, 'aweme_list': [
            {'aweme_id': '1234567890123456789', 'video': {'play_addr': {'url_list': [MEDIA]}}}]})
        with self.assertRaises(ValueError):
            anonymous.douyin_metadata(DOUYIN)

    @patch('anonymous_sources.public_get')
    def test_douyin_business_error_does_not_return_media(self, get):
        get.return_value = response({'status_code': 2483, 'aweme_list': []})
        with self.assertRaises(ValueError):
            anonymous.douyin_metadata(DOUYIN)

    @patch('anonymous_sources.public_get')
    def test_douyin_target_without_media_is_failure(self, get):
        for urls in ([], [''], ['file:///private/audio.mp4']):
            get.return_value = response({'status_code': 0, 'aweme_list': [{
                'aweme_id': VID, 'video': {'play_addr': {'url_list': urls}}}]})
            with self.subTest(urls=urls), self.assertRaises(ValueError):
                anonymous.douyin_metadata(DOUYIN)

    @patch('anonymous_sources.public_get')
    def test_douyin_malformed_video_is_controlled_failure(self, get):
        for media in ('not-an-object', {'play_addr': 'not-an-object'}):
            get.return_value = response({'status_code': 0, 'aweme_list': [{
                'aweme_id': VID, 'video': media}]})
            with self.subTest(media=media), self.assertRaises(ValueError):
                anonymous.douyin_metadata(DOUYIN)

    @patch('anonymous_sources.public_get')
    def test_external_disabled_does_not_contact_service(self, get):
        with self.assertRaises(ValueError):
            anonymous.api_metadata(CHANNELS, '视频号', config(enabled=False))
        get.assert_not_called()

    @patch('anonymous_sources.public_get')
    def test_external_unapproved_platform_does_not_contact_service(self, get):
        with self.assertRaises(ValueError):
            anonymous.api_metadata(CHANNELS, '视频号', config(platforms=['抖音']))
        get.assert_not_called()

    @patch('anonymous_sources.public_get')
    def test_external_never_inherits_operator_cookie_or_auth(self, get):
        get.return_value = response(video())
        cfg = config()
        cfg.update({'cookie': 'private-cookie', 'authorization': 'private-token'})
        result = anonymous.api_metadata(CHANNELS, '视频号', cfg)
        self.assertEqual(result['title'], '公共样本')
        self.assertEqual(result['author'], '公开作者')
        self.assertEqual(result['media_url'], MEDIA)
        self.assertEqual(get.call_count, 1)
        args, kwargs = get.call_args
        self.assertEqual(kwargs.get('params', {}).get('url'), CHANNELS)
        self.assertNotIn('cookies', kwargs)
        self.assertNotIn('auth', kwargs)
        self.assertFalse({'cookie', 'authorization'} & {k.lower() for k in kwargs.get('headers', {})})
        self.assertNotIn('private-cookie', str(get.call_args))
        self.assertNotIn('private-token', str(get.call_args))

    @patch('anonymous_sources.public_get')
    def test_external_business_error_is_not_success(self, get):
        get.return_value = response({'code': 500, 'msg': '失效', 'data': video()['data']})
        with self.assertRaises(ValueError):
            anonymous.api_metadata(CHANNELS, '视频号', config())

    @patch('anonymous_sources.public_get')
    def test_external_business_error_does_not_echo_service_secrets(self, get):
        get.return_value = response({'code': 500, 'msg': 'Cookie: private-cookie token=private-token'})
        with self.assertRaises(ValueError) as raised:
            anonymous.api_metadata(CHANNELS, '视频号', config())
        self.assertNotIn('private-cookie', str(raised.exception))
        self.assertNotIn('private-token', str(raised.exception))

    @patch('anonymous_sources.public_get')
    def test_external_rejects_credentialed_endpoint_before_network(self, get):
        with self.assertRaises(ValueError):
            anonymous.api_metadata(CHANNELS, '视频号', config(endpoint='https://user:password@api.example/parse'))
        get.assert_not_called()

    @patch('anonymous_sources.public_get')
    def test_external_image_post_is_not_an_audio_transcript(self, get):
        for post_type in ('image', 'images'):
            with self.subTest(type=post_type):
                get.return_value = response(video(type=post_type))
                with self.assertRaises(ValueError):
                    anonymous.api_metadata(CHANNELS, '视频号', config())

    @patch('anonymous_sources.public_get')
    def test_external_malformed_payload_is_controlled_failure(self, get):
        for payload in ([], None, {'code': 200, 'data': []}, {'code': 200, 'data': None}):
            with self.subTest(payload=payload):
                get.return_value = response(payload)
                with self.assertRaises(ValueError):
                    anonymous.api_metadata(CHANNELS, '视频号', config())

    @patch('anonymous_sources.public_get')
    def test_external_missing_opt_in_does_not_contact_service(self, get):
        with self.assertRaises(ValueError):
            anonymous.api_metadata(CHANNELS, '视频号', {})
        get.assert_not_called()

    @patch('anonymous_sources.public_get')
    def test_external_empty_media_is_failure(self, get):
        get.return_value = response(video(url=''))
        with self.assertRaises(ValueError):
            anonymous.api_metadata(CHANNELS, '视频号', config())

    @patch('anonymous_sources.public_get')
    def test_external_alternate_video_url_field(self, get):
        get.return_value = response(video(url='', video_url=MEDIA))
        self.assertEqual(anonymous.api_metadata(CHANNELS, '视频号', config())['media_url'], MEDIA)

    @patch('anonymous_sources.public_get')
    def test_external_rejects_non_http_media(self, get):
        get.return_value = response(video(url='file:///C:/private.txt'))
        with self.assertRaises(ValueError):
            anonymous.api_metadata(CHANNELS, '视频号', config())

    @patch('anonymous_sources.public_get')
    def test_transport_error_does_not_leak_query_or_credentials(self, get):
        get.side_effect = requests.RequestException(
            'https://someone:password@service.example/api?token=secret Cookie: personal-cookie')
        with self.assertRaises(ValueError) as raised:
            anonymous.api_metadata(CHANNELS, '视频号', config())
        message = str(raised.exception)
        for sensitive in ('password', 'secret', 'personal-cookie'):
            self.assertNotIn(sensitive, message)


if __name__ == '__main__':
    unittest.main()
