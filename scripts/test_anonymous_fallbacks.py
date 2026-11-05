import unittest
from unittest.mock import Mock, patch
import anonymous_sources as anonymous
from urllib.parse import parse_qs,urlsplit


class AnonymousFallbackTests(unittest.TestCase):
    @patch('anonymous_sources.public_get')
    def test_youtube_without_corresponding_proxy_signature_is_rejected(self,get):
        config=self.config();config['anonymous_api'].update(endpoint='https://ynvan.com/api/parse',schema='ynvan',fallbacks=[])
        media='https://cdn.googlevideo.com/videoplayback?expire=1'
        for signatures in ({}, {'https://different.example/media':'not-the-signature'}, {media:''}, {media:123}):
            get.return_value.json.return_value={'code':200,'data':{
                'content_kind':'media','title':'Target video',
                'video_url':media,'sig':signatures}}
            with self.subTest(signatures_type=type(signatures).__name__),self.assertRaises(ValueError):
                anonymous.api_metadata('https://www.youtube.com/watch?v=target','YouTube',config)

    def config(self):
        return {'anonymous_api':{'enabled':True,'platforms':['YouTube'],
            'fallbacks':[{'endpoint':'https://ynvan.com/api/parse','schema':'ynvan','platforms':['YouTube']}]}}

    @patch('anonymous_sources.public_get')
    def test_service_failure_uses_only_configured_eligible_fallback(self,get):
        first=Mock();first.json.return_value={'code':501,'data':{}}
        second=Mock();second.json.return_value={'code':200,'data':{
            'content_kind':'media','source':'youtube','title':'Target video',
            'video_url':'https://video.test/target.webm','author':{'name':'Author'},
            'sig':{'https://video.test/target.webm':'service-signature'}}}
        get.side_effect=[first,second]
        result=anonymous.api_metadata('https://www.youtube.com/watch?v=target','YouTube',self.config())
        self.assertEqual(result['resolver_service'],'ynvan.com')
        self.assertEqual(get.call_count,2)
        self.assertEqual(result['acquisition_attempts'][0]['status'],'failed')

    @patch('anonymous_sources.public_get')
    def test_signed_proxy_uses_only_service_returned_signature(self,get):
        config=self.config();config['anonymous_api']['endpoint']='https://ynvan.com/api/parse'
        config['anonymous_api']['schema']='ynvan';config['anonymous_api']['fallbacks']=[]
        media='https://googlevideo.example/video?expire=1&signature=public'
        get.return_value.json.return_value={'code':200,'data':{
            'content_kind':'media','source':'youtube','title':'Target video','page_url':'https://www.youtube.com/watch?v=target',
            'video_url':media,'sig':{media:'service-issued-signature'}}}
        result=anonymous.api_metadata('https://www.youtube.com/watch?v=target','YouTube',config)
        parsed=urlsplit(result['media_url'])
        self.assertEqual(parsed.hostname,'ynvan.com')
        self.assertEqual(parse_qs(parsed.query)['sig'],['service-issued-signature'])
        self.assertEqual(parse_qs(parsed.query)['url'],[media])
        self.assertEqual(result['transfer'],'curl')

    @patch('anonymous_sources.public_get')
    def test_wrong_youtube_page_is_not_accepted(self,get):
        config=self.config();config['anonymous_api']['endpoint']='https://ynvan.com/api/parse'
        config['anonymous_api']['schema']='ynvan';config['anonymous_api']['fallbacks']=[]
        get.return_value.json.return_value={'code':200,'data':{
            'content_kind':'media','title':'Wrong video','page_url':'https://www.youtube.com/watch?v=not-target',
            'video_url':'https://video.test/wrong.mp4'}}
        with self.assertRaises(ValueError):
            anonymous.api_metadata('https://www.youtube.com/watch?v=target','YouTube',config)

    @patch('anonymous_sources.public_get')
    def test_cover_only_fallback_is_not_media_success(self,get):
        first=Mock();first.json.return_value={'code':501}
        second=Mock();second.json.return_value={'code':200,'data':{
            'content_kind':'media','title':'Only cover','cover_url':'https://cover.test/c.jpg','video_url':''}}
        get.side_effect=[first,second]
        with self.assertRaises(ValueError):
            anonymous.api_metadata('https://www.youtube.com/watch?v=target','YouTube',self.config())
        self.assertEqual(get.call_count,2)

    @patch('anonymous_sources.public_get')
    def test_all_services_failure_is_bounded(self,get):
        get.return_value.json.return_value={'code':501}
        with self.assertRaises(ValueError):
            anonymous.api_metadata('https://www.youtube.com/watch?v=target','YouTube',self.config())
        self.assertEqual(get.call_count,2)


if __name__=='__main__':unittest.main()
