"""Input, media-handoff and queue contracts; fixtures contain no real sample links."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import batch_jobs
import documents as d
import failures
import inputs
import resources
import sources
import anonymous_sources as anonymous
import transcribe as cli


class InputTests(unittest.TestCase):
    def test_share_text_and_markdown_preserve_signed_query_bytes(self):
        url='https://media.example/audio?sig=a%2Bb%3D&x=1&x=2'
        self.assertEqual(inputs.normalize('复制打开平台 看作品 '+url+' 直达全文'),url)
        self.assertEqual(inputs.normalize('['+url+']('+url+')'),url)
        self.assertEqual(inputs.normalize(url),url)
        self.assertEqual(inputs.normalize(url+'&opaque=ending)'),url+'&opaque=ending)')
        with self.assertRaises(ValueError):inputs.normalize(url+' https://other.example/video')
        with self.assertRaises(ValueError):inputs.normalize('https://name:password@media.example/audio')

    def test_local_paths_with_spaces_and_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'中文 录音.wav';p.write_bytes(b'fixture')
            self.assertEqual(inputs.normalize('"'+str(p)+'"'),str(p.resolve()))

    def test_known_id_aliases_and_part_boundaries(self):
        self.assertEqual(inputs.key('https://youtu.be/DEMO_ID_001'),inputs.key('https://www.youtube.com/watch?v=DEMO_ID_001&t=20'))
        self.assertEqual(inputs.key('https://www.douyin.com/video/1234567890123456789'),inputs.key('https://www.douyin.com/?modal_id=1234567890123456789'))
        self.assertNotEqual(inputs.key('https://www.bilibili.com/video/BVDEMO?p=1'),inputs.key('https://www.bilibili.com/video/BVDEMO?p=2'))
        self.assertNotEqual(inputs.key('https://media.example/audio?sig=one'),inputs.key('https://media.example/audio?sig=two'))
        self.assertNotEqual(inputs.key('https://youtu.be/DEMO_ID_001'),inputs.key('https://youtu.be/DEMO_ID_001',10))
        self.assertTrue(inputs.identity('https://www.youtube.com/results').startswith('url:'))

    def test_old_exact_input_job_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            url='https://www.youtube.com/watch?v=DEMO_ID_001'
            legacy=Path(tmp)/'jobs'/hashlib.sha256((url+'|limit=None').encode()).hexdigest()[:20]
            legacy.mkdir(parents=True);d.write_json(legacy/'manifest.json',{'input':url})
            self.assertEqual(inputs.job_path(url,tmp),legacy)


class PlatformInputTests(unittest.TestCase):
    def test_douyin_modal_link_uses_explicit_item_without_fetching_homepage(self):
        vid='1234567890123456789'
        response=Mock()
        response.json.return_value={'status_code':0,'aweme_list':[{'aweme_id':vid,'desc':'Synthetic','video':{'duration':1000,'play_addr':{'url_list':['https://media.example/fixture.mp4']}}}]}
        with patch('anonymous_sources.public_get',return_value=response) as get:
            info=anonymous.douyin_metadata('https://www.douyin.com/?modal_id='+vid)
        get.assert_called_once();self.assertEqual(info['id'],vid);self.assertEqual(get.call_args.kwargs['params']['aweme_id'],vid)
        self.assertIsNone(anonymous._native_id('https://www.douyin.com/note/987?modal_id='+vid))
        self.assertEqual(sources.platform('https://www.amemv.com/share/video/'+vid),'抖音')

    def test_rate_limit_does_not_fan_out_to_other_resolvers(self):
        import requests
        response=requests.Response();response.status_code=429;response.url='https://media.example/endpoint';response._content=b'Limited'
        cfg={'anonymous_api':{'enabled':True,'platforms':['抖音','YouTube'],'fallbacks':[{'endpoint':'https://backup.example/parse','platforms':['YouTube']}]}}
        with patch('anonymous_sources.public_get',return_value=response) as get,patch('sources._metadata_native') as native:
            with self.assertRaises(ValueError) as raised:sources.metadata('https://www.douyin.com/video/1234567890123456789',config=cfg)
        self.assertEqual(failures.describe(raised.exception)['error_code'],'rate_limited');get.assert_called_once();native.assert_not_called()
        with patch('anonymous_sources.public_get',return_value=response) as get:
            with self.assertRaises(ValueError):anonymous.api_metadata('https://www.youtube.com/watch?v=DEMO_ID_001','YouTube',cfg)
        get.assert_called_once()

    def test_resolver_budget_failure_is_not_swallowed(self):
        cfg={'anonymous_api':{'enabled':True,'platforms':['YouTube']}}
        with patch('anonymous_sources.public_get',side_effect=resources.ResourceLimit('deadline')) as get:
            with self.assertRaises(resources.ResourceLimit):anonymous.api_metadata('https://www.youtube.com/watch?v=DEMO_ID_001','YouTube',cfg)
        get.assert_called_once()
        with patch('sources._metadata_native',side_effect=ValueError('native failed')),patch('anonymous_sources.api_metadata',side_effect=resources.ResourceLimit('deadline')):
            with self.assertRaises(resources.ResourceLimit):sources.metadata('https://www.youtube.com/watch?v=DEMO_ID_001',config=cfg)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.media=self.root/'中文 录音.wav'
        with wave.open(str(self.media),'wb') as f:
            f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\x10\0'*16000)
        self.config=dict(runtime=str(self.root),output=str(self.root/'out'))

    def args(self,prepare=True):
        return argparse.Namespace(input=str(self.media),job=None,limit_seconds=None,output=None,cookies=None,source_url=None,title=None,speakers='auto',speaker_evidence='',prepare_only=prepare)

    def test_prepare_decodes_without_models_and_reuses_verified_audio(self):
        with patch('engine.asr') as asr,patch('engine.diarize') as diarize:
            result=cli.run(self.args(),self.config)
        asr.assert_not_called();diarize.assert_not_called()
        self.assertEqual(result['status'],'audio_ready')
        job=Path(result['job']);receipt=d.read_json(result['receipt'])
        self.assertEqual(receipt['audio_sha256'],d.file_hash(result['audio_path']))
        self.assertEqual(receipt['duration_seconds'],1.)
        self.assertFalse(d.read_json(job/'manifest.json')['coverage_complete'])
        with patch('sources.acquire') as acquire,patch('sources.convert') as convert:
            second=cli.run(self.args(),self.config)
        acquire.assert_not_called();convert.assert_not_called();self.assertEqual(second['job'],result['job'])

    def test_run_after_prepare_and_reprepare_keep_review_state(self):
        prepared=cli.run(self.args(),self.config)
        asr={'chunks':[dict(start=0,end=1,text='测试',tokens=['测','试'],timestamps=[.1,.5])],'elapsed_this_run':0}
        with patch('engine.asr',return_value=asr),patch('engine.diarize',return_value={'segments':[],'report':{'count_mode':'unavailable'}}),patch('sources.acquire') as acquire:
            result=cli.run(self.args(False),self.config)
        acquire.assert_not_called();self.assertEqual(result['job'],prepared['job'])
        self.assertEqual(result['status'],'review_pending')
        cli.run(self.args(),self.config)
        manifest=d.read_json(Path(result['job'])/'manifest.json')
        self.assertEqual(manifest['status'],'review_pending');self.assertTrue(manifest['coverage_complete'])

    def test_cli_stdout_is_single_json_including_failures(self):
        entry=Path(cli.__file__)
        process=subprocess.run([sys.executable,'-X','utf8',str(entry),'prepare',str(self.media),'--job',str(self.root/'cli-job')],capture_output=True,text=True,encoding='utf-8',timeout=45)
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(json.loads(process.stdout)['status'],'audio_ready')
        failed=subprocess.run([sys.executable,'-X','utf8',str(entry),'prepare','not an input'],capture_output=True,text=True,encoding='utf-8',timeout=45)
        self.assertNotEqual(failed.returncode,0);self.assertEqual(json.loads(failed.stdout)['error_code'],'invalid_input')

    def test_real_http_acquisition_conversion_and_rate_limit(self):
        body=self.media.read_bytes()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/limited':self.send_response(429);self.end_headers();return
                self.send_response(200);self.send_header('Content-Type','audio/wav');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for suffix in ('/audio','/limited'):
                args=self.args();args.input='https://media.example'+suffix
                info=dict(id='synthetic',title='Synthetic',source_url=args.input,platform='测试',duration=1,acquisition='configured_resolver',media_url=f'http://127.0.0.1:{server.server_port}'+suffix)
                with patch('sources.metadata',return_value=info) as metadata:
                    if suffix=='/limited':
                        with self.assertRaises(ValueError):cli.run(args,self.config)
                        job=inputs.job_path(args.input,self.config['runtime']);m=d.read_json(job/'manifest.json')
                        self.assertEqual(m['error_code'],'rate_limited');self.assertEqual(len(m['download_attempts']),1)
                    else:self.assertEqual(cli.run(args,self.config)['audio_seconds'],1.)
                metadata.assert_called_once()
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def batch_args(self,file,**kwargs):
        return argparse.Namespace(file=str(file),max_items=20,prepare_only=True,dry_run=False,retry_failed=False,limit_seconds=None,output=None,cookies=None,**kwargs)

    def test_batch_failure_isolation_resume_and_additions(self):
        file=self.root/'links.json';file.write_text(json.dumps([str(self.media),'invalid input',str(self.media)]),encoding='utf-8')
        args=self.batch_args(file)
        first=batch_jobs.execute(args,self.config,cli.run,cli.job_lock)
        self.assertEqual((first['ready'],first['failed']),(1,1))
        state=d.read_json(first['queue']);self.assertEqual(len(state['items']),2)
        self.assertNotIn('complete',[i['status'] for i in state['items'].values()])
        second=batch_jobs.execute(args,self.config,cli.run,cli.job_lock)
        self.assertEqual(second['queue'],first['queue'])
        args.retry_failed=True
        third=batch_jobs.execute(args,self.config,cli.run,cli.job_lock)
        self.assertEqual((third['ready'],third['failed']),(1,1))
        file.write_text(json.dumps([str(self.media)]),encoding='utf-8')
        fourth=batch_jobs.execute(args,self.config,cli.run,cli.job_lock)
        self.assertEqual(fourth['status'],'batch_ready');self.assertEqual(fourth['queue'],first['queue'])

    def test_batch_dry_run_does_not_create_queue_or_acquire(self):
        file=self.root/'links.txt';file.write_text(str(self.media)+'\n'+str(self.media),encoding='utf-8')
        args=self.batch_args(file);args.dry_run=True
        with patch('sources.acquire') as acquire:
            preview=batch_jobs.execute(args,self.config,cli.run,cli.job_lock)
        self.assertEqual(preview['count'],1);self.assertFalse((self.root/'queues').exists());acquire.assert_not_called()

    def test_malformed_batch_input_has_an_input_diagnostic(self):
        file=self.root/'broken.json';file.write_text('[broken',encoding='utf-8')
        with self.assertRaises(ValueError) as raised:batch_jobs.load(file,20)
        self.assertEqual(failures.describe(raised.exception)['error_code'],'invalid_input')

    def test_queue_retries_only_when_requested_and_recovers_processing(self):
        file=self.root/'links.txt';file.write_text('https://media.example/audio',encoding='utf-8');args=self.batch_args(file)
        with patch('sources.metadata',side_effect=ValueError('HTTP 404')) as metadata:
            first=batch_jobs.execute(args,self.config,cli.run,cli.job_lock)
            batch_jobs.execute(args,self.config,cli.run,cli.job_lock)
            metadata.assert_called_once()
            args.retry_failed=True;batch_jobs.execute(args,self.config,cli.run,cli.job_lock)
            self.assertEqual(metadata.call_count,2)
        state=d.read_json(first['queue']);item=next(iter(state['items'].values()));item['status']='processing';d.write_json(first['queue'],state)
        with patch('sources.metadata',side_effect=ValueError('HTTP 404')) as metadata:
            args.retry_failed=False;batch_jobs.execute(args,self.config,cli.run,cli.job_lock)
        metadata.assert_called_once()

    def test_dependency_error_and_failed_stage_are_actionable(self):
        args=self.args(False)
        with patch('engine.asr',side_effect=FileNotFoundError('missing model')),patch('engine.diarize',return_value={'segments':[],'report':{}}):
            with self.assertRaises(ValueError) as raised:cli.run(args,self.config)
        self.assertEqual(failures.describe(raised.exception)['error_code'],'dependency_missing')
        manifest=d.read_json(inputs.job_path(args.input,self.config['runtime'])/'manifest.json')
        self.assertEqual(manifest['failed_stage'],'transcribing')

    def test_native_downloader_path_escape_is_rejected(self):
        result={'requested_downloads':[{'filepath':str(self.media)}]}
        with patch('yt_dlp.YoutubeDL') as factory:
            downloader=factory.return_value.__enter__.return_value;downloader.extract_info.return_value=result;downloader.prepare_filename.return_value=str(self.media)
            job=self.root/'download';job.mkdir()
            with self.assertRaisesRegex(ValueError,'目录外'):sources.acquire({'resolved_url':'https://media.example/video'},job)
        self.assertTrue(self.media.exists())


if __name__=='__main__':unittest.main()
