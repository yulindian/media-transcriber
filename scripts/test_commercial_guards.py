"""Synthetic regression cases for entity, provenance, quality and resource gates."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor

import documents as d
import privacy
import public_http
import quality
import resources
import sources
import transcribe


class CommercialGuards(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.job=Path(self.tmp.name)/'job';self.job.mkdir()
        self.turns=[dict(id='t1',start=0.,end=1.,speaker='说话人1',text='金额是100元。')]
        d.write_json(self.job/'source.json',dict(id='fixture',title='Synthetic',source_url='https://example.com/audio?token=PRIVATE_VALUE'))
        d.write_json(self.job/'manifest.json',dict(created_at='2026-11-07',audio_duration=1.,coverage_complete=True,status='review_pending'))
        d.reset_review(self.job,self.turns)
        with wave.open(str(self.job/'audio.wav'),'wb') as f:
            f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\x01\x00'*16000)

    def edit(self,before,after):
        return dict(turn_id='t1',before=before,after=after,reason='Synthetic correction',evidence='Synthetic assertion')

    def reviewed(self):
        d.save_review(self.job,self.turns,dict(reviewed_ids=['t1'],edits=[]))

    def accept(self):
        report=quality.check(self.job)
        quality.accept(self.job,dict(snapshot_fingerprint=report['snapshot_fingerprint'],accepted_issues=[dict(turn_id=i['turn_id'],reason='Synthetic known ambiguity retained') for i in report['issues']]))

    def test_numeric_dates_percent_and_units_require_separate_evidence(self):
        for a,b in [('100元','1000元'),('一百元','一千元'),('3.5%','35%'),('2026年11月7日','2026年11月8日'),('3公里','3米'),('百分之十','百分之二十'),('3 kg','3 g'),('30℃','30℉')]:
            t=[dict(self.turns[0],text='数值是'+a+'。')]
            report=d.preflight_review(t,dict(reviewed_ids=['t1'],edits=[self.edit(t[0]['text'],'数值是'+b+'。')]),self.job)
            with self.subTest(a=a,b=b):self.assertFalse(report['valid'])

    def test_punctuation_preserves_entities_without_audio_review(self):
        t=[dict(self.turns[0],text='100元 3小时')]
        new,_,_=d.apply_review(t,dict(reviewed_ids=['t1'],edits=[self.edit(t[0]['text'],'100元，3小时。')]))
        self.assertEqual(d.entities(new[0]['text']),d.entities(t[0]['text']))

    def entity_edit(self):
        clip=quality.clip(self.job,'t1',0)
        e=self.edit(self.turns[0]['text'],'金额是1000元。')
        e['entity_review']=dict(method='audio_excerpt_review',clip_path=clip['path'],quote=e['after'],note='Synthetic fixture, explicit audio-review assertion')
        return e

    def test_bound_entity_review_and_audit_are_preserved(self):
        e=self.entity_edit()
        new,edits,ids=d.apply_review(self.turns,dict(reviewed_ids=['t1'],edits=[e]),self.job)
        d.save_review(self.job,new,dict(reviewed_ids=ids,edits=edits))
        self.assertEqual(d.read_json(self.job/'review-state.json')['edits'][0]['entity_review'],e['entity_review'])
        self.assertNotIn('unverified_historical_entity_change',quality.inspect(self.job)['counts'])
        self.accept()
        Path(e['entity_review']['clip_path']).unlink()
        with self.assertRaisesRegex(ValueError,'过期'):d.finalize(self.job,self.job/'out')
        with self.assertRaises(ValueError):d.apply_review(self.turns,dict(reviewed_ids=['t1'],edits=[e]))

    def test_changed_audio_or_turn_invalidates_entity_evidence(self):
        for mode in ('audio','turn','clip'):
            e=self.entity_edit();turns=copy.deepcopy(self.turns)
            if mode=='audio':
                with (self.job/'audio.wav').open('ab') as f:f.write(b'changed')
            elif mode=='turn':turns[0]['text']+='嗯';e['before']=turns[0]['text']
            else:Path(e['entity_review']['clip_path']).write_bytes(b'changed')
            with self.subTest(mode=mode),self.assertRaises(ValueError):d.apply_review(turns,dict(reviewed_ids=['t1'],edits=[e]),self.job)

    def test_export_requires_current_check_and_acceptance(self):
        self.reviewed()
        with self.assertRaisesRegex(ValueError,'quality'):d.finalize(self.job,self.job/'out')
        quality.check(self.job)
        with self.assertRaisesRegex(ValueError,'accept-quality'):d.finalize(self.job,self.job/'out')
        self.accept()
        result=d.finalize(self.job,self.job/'out')
        self.assertEqual(result['verification_scope'],'artifact_integrity')
        self.assertEqual(result['quality_status'],'accepted_no_detected_issues')
        self.assertNotIn('PRIVATE_VALUE',Path(result['path']).read_text(encoding='utf-8'))
        self.assertEqual(d.finalize(self.job,self.job/'out')['status'],'existing')

    def test_any_audio_source_or_review_change_stales_acceptance(self):
        self.reviewed()
        for name in ('audio.wav','source.json','review-state.json'):
            self.accept();file=self.job/name;old=file.read_bytes()
            if file.suffix=='.json':
                data=d.read_json(file);data['extra']='changed';d.write_json(file,data)
            else:file.write_bytes(old+b'changed')
            with self.subTest(file=name),self.assertRaisesRegex(ValueError,'过期'):d.finalize(self.job,self.job/'out')
            file.write_bytes(old)

    def test_ambiguous_speaker_needs_disclosed_acceptance(self):
        self.turns[0]['speaker']='说话人未确定';d.reset_review(self.job,self.turns);self.reviewed()
        report=quality.check(self.job)
        with self.assertRaises(ValueError):quality.accept(self.job,dict(snapshot_fingerprint=report['snapshot_fingerprint'],accepted_issues=[]))
        self.accept();result=d.finalize(self.job,self.job/'out')
        self.assertEqual(result['quality_status'],'accepted_with_disclosed_issues')
        log=d.read_json(result['correction_log'])
        self.assertTrue(log['quality_acceptance']['accepted_issues'][0]['reason'])

    def test_zero_duration_is_not_an_acceptable_doubt(self):
        self.turns[0]['end']=0.;d.reset_review(self.job,self.turns);self.reviewed()
        with self.assertRaisesRegex(ValueError,'零时长'):self.accept()

    def test_long_unpunctuated_suffix_is_detected(self):
        self.turns[0]['text']='开头。'+'后面的长内容'*40;d.reset_review(self.job,self.turns)
        self.assertEqual(quality.inspect(self.job)['counts']['long_unpunctuated_turn'],1)

    def test_unprotected_legacy_entity_edit_is_disclosed(self):
        after=[dict(self.turns[0],text='金额是1000元。')]
        d.write_json(self.job/'turns.corrected.json',after)
        d.write_json(self.job/'review-state.json',dict(reviewed_ids=['t1'],edits=[self.edit(self.turns[0]['text'],after[0]['text'])]))
        self.assertEqual(quality.inspect(self.job)['counts']['unverified_historical_entity_change'],1)

    def test_provenance_removes_secrets_and_keeps_only_public_identity(self):
        url='https://user:secret@www.youtube.com/watch?v=DEMO_ID&token=PRIVATE_VALUE#secret'
        self.assertEqual(privacy.source_url(url),'https://www.youtube.com/watch?v=DEMO_ID')
        self.assertEqual(privacy.source_url('https://example.com/audio?key=PRIVATE_VALUE'),'https://example.com/audio')
        self.assertEqual(privacy.source_url('C:/Users/private/recording.wav'),'[local source]')
        self.assertEqual(privacy.source_url('https://www.bilibili.com/video/BV_DEMO?p=2&sign=PRIVATE_VALUE'),'https://www.bilibili.com/video/BV_DEMO?p=2')
        original=dict(before='原文网址 https://example.com/?token=spoken',evidence='来源 https://example.com/?token=PRIVATE_VALUE',entity_review=dict(clip_path='C:/Users/private/job/review-clips/t1.wav'))
        safe=privacy.audit(original)
        self.assertEqual(safe['before'],original['before'])
        self.assertNotIn('PRIVATE_VALUE',json.dumps(safe))
        self.assertEqual(safe['entity_review']['clip_path'],'review-clips/t1.wav')

    def test_invalid_preflight_exits_nonzero(self):
        file=self.job/'bad.json';d.write_json(file,dict(reviewed_ids=['t1'],edits=[self.edit('STALE','金额是100元。')]))
        p=subprocess.run([sys.executable,'-X','utf8',str(Path(transcribe.__file__)),'apply-edits',str(self.job),'--input',str(file),'--check'],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(p.returncode,2);self.assertFalse(json.loads(p.stdout)['valid'])

    def test_download_limit_aborts_unknown_length_and_removes_partial(self):
        response=Mock(status_code=200,headers={'Content-Type':'audio/wav'})
        response.iter_content.return_value=iter([b'abc',b'def',b'ghi'])
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        info=dict(media_url='https://example.com/audio.wav',acquisition='native')
        with resources.scope({'limits':{'max_download_bytes':5}}),patch('sources.public_get',return_value=response):
            with self.assertRaises(resources.ResourceLimit):sources.acquire(info,self.job)
        self.assertFalse((self.job/'source.wav').exists());self.assertFalse(list(self.job.glob('*.part')))

    def test_metadata_limit_applies_while_streaming(self):
        import requests
        response=requests.Response();response.status_code=200;response.url='https://example.com/';response.raw=Mock()
        consumed=[]
        def chunks(*args):
            for _ in range(4):consumed.append(1);yield b'x'*5
        response.iter_content=chunks
        session=Mock();session.get.return_value=response
        with patch('public_http.requests.Session',return_value=session),patch('public_http.getproxies',return_value={}),patch('public_http.MAX_NON_STREAM_BYTES',8):
            with self.assertRaises(ValueError):public_http.get('https://example.com/')
        self.assertEqual(len(consumed),2)
        self.assertTrue(session.get.call_args.kwargs['stream'])

    def test_convert_timeout_cleans_only_temporary_output(self):
        target=self.job/'converted.wav';target.write_bytes(b'EXISTING')
        def fail(cmd,**kwargs):
            Path(cmd[-1]).write_bytes(b'PARTIAL')
            self.assertGreater(kwargs['timeout'],0)
            raise subprocess.TimeoutExpired(cmd,1)
        with patch('sources.subprocess.run',side_effect=fail),self.assertRaises(resources.ResourceLimit):sources.convert(self.job/'audio.wav',target)
        self.assertEqual(target.read_bytes(),b'EXISTING');self.assertFalse((self.job/'converted.converting.wav').exists())

    def test_actual_ffmpeg_audio_cap_then_resume(self):
        target=self.job/'converted.wav'
        with resources.scope({'limits':{'max_audio_seconds':.5}}),self.assertRaises(resources.ResourceLimit):sources.convert(self.job/'audio.wav',target)
        self.assertFalse(target.exists())
        with resources.scope({'limits':{'max_audio_seconds':2}}):sources.convert(self.job/'audio.wav',target)
        with wave.open(str(target)) as f:self.assertEqual(f.getnframes(),16000)

    def test_disk_and_deadline_and_memory_fail_before_work(self):
        with patch('resources.shutil.disk_usage',return_value=SimpleNamespace(free=1)),self.assertRaises(resources.ResourceLimit):resources.disk(self.job)
        with resources.scope({'limits':{'max_waveform_bytes':100}}),self.assertRaises(resources.ResourceLimit):resources.audio(10,parallel=True)
        with patch('resources.time.monotonic',return_value=0):
            with resources.scope({'limits':{'task_seconds':1}}):
                with patch('resources.time.monotonic',return_value=2),self.assertRaises(resources.ResourceLimit):resources.remaining()

    def test_budget_reaches_worker_thread_and_does_not_leak(self):
        with resources.scope({'limits':{'max_download_bytes':5}}),ThreadPoolExecutor() as pool:
            self.assertEqual(resources.submit(pool,lambda:resources.policy()['max_download_bytes']).result(),5)
        self.assertEqual(resources.policy()['max_download_bytes'],resources.DEFAULTS['max_download_bytes'])

    def test_resource_failure_does_not_refresh_or_retry_source(self):
        job=Path(self.tmp.name)/'limited-job'
        args=SimpleNamespace(input='https://example.com/audio.wav',job=str(job),limit_seconds=None,output=None,cookies=None,source_url=None,title=None,speakers='auto',speaker_evidence='')
        descriptor=dict(id='fixture',title='Synthetic',platform='其他平台',source_url=args.input,media_url=args.input)
        with patch('sources.metadata',return_value=descriptor) as metadata,patch('sources.acquire',side_effect=resources.ResourceLimit('budget')) as acquire:
            with self.assertRaisesRegex(ValueError,'budget'):transcribe.run(args,dict(runtime=self.tmp.name,output=str(job/'out')))
        metadata.assert_called_once();acquire.assert_called_once()
        self.assertEqual(d.read_json(job/'manifest.json')['status'],'failed')

    def test_metadata_budget_failure_does_not_use_anonymous_fallback(self):
        cfg={'anonymous_api':{'enabled':True,'platforms':['YouTube']}}
        with patch('sources._metadata_native',side_effect=resources.ResourceLimit('deadline')),patch('anonymous_sources.api_metadata') as alternate:
            with self.assertRaises(resources.ResourceLimit):sources.metadata('https://www.youtube.com/watch?v=SYNTHETIC',config=cfg)
        alternate.assert_not_called()

    def test_interrupted_export_cannot_publish_after_source_changes(self):
        self.reviewed();self.accept();out=self.job/'out';original=d._write_text
        def fail(path,text):
            if Path(path).parent==out:raise OSError('synthetic interruption')
            return original(path,text)
        with patch.object(d,'_write_text',side_effect=fail),self.assertRaises(OSError):d.finalize(self.job,out)
        source=d.read_json(self.job/'source.json');source['title']='Changed';d.write_json(self.job/'source.json',source)
        with self.assertRaisesRegex(ValueError,'过期'):d.finalize(self.job,out)
        self.assertEqual(list(out.iterdir()),[])

    def test_legacy_export_journal_is_not_published_unchecked(self):
        out=self.job/'out'
        d._write_transaction(self.job/'export-transaction.json',{'files':[dict(path=str(out/'old.md'),text='private legacy text')],'manifest_update':{}})
        with self.assertRaisesRegex(ValueError,'旧版'):d.finalize(self.job,out)
        self.assertFalse(out.exists())

    def test_full_state_workflow_with_real_conversion_and_synthetic_asr(self):
        job=Path(self.tmp.name)/'pipeline'
        args=SimpleNamespace(input=str(self.job/'audio.wav'),job=str(job),limit_seconds=None,output=None,cookies=None,source_url=None,title=None,speakers='auto',speaker_evidence='')
        text='金额是100元'
        asr=dict(chunks=[dict(start=0.,end=1.,text=text,tokens=list(text),timestamps=[.1+i*.1 for i in range(len(text))])],elapsed_this_run=0)
        speakers=dict(segments=[dict(start=0.,end=1.,speaker=0,margin=1.)],report=dict(count=1,count_mode='estimated'))
        with patch('engine.asr',return_value=asr),patch('engine.diarize',return_value=speakers):
            result=transcribe.run(args,dict(runtime=self.tmp.name,output=str(job/'out')))
        self.assertEqual(result['status'],'review_pending')
        batch=transcribe.review(SimpleNamespace(job=str(job),offset=0,count=60,max_chars=6000,next=True,verbose=False))
        t=batch['turns'][0];payload=job/'edits.json'
        d.write_json(payload,dict(reviewed_ids=[t['id']],edits=[dict(turn_id=t['id'],before=t['text'],after=t['text']+'。',reason='Punctuation',evidence='Synthetic sentence')]))
        transcribe.edit(SimpleNamespace(job=str(job),input=str(payload),check=False))
        report=quality.check(job)
        quality.accept(job,dict(snapshot_fingerprint=report['snapshot_fingerprint'],accepted_issues=[]))
        exported=transcribe.finalize_job(SimpleNamespace(job=str(job),output=None),dict(output=str(job/'out')))
        self.assertTrue(exported['verified'])
        self.assertEqual(Path(exported['path']).parent,job/'out')
        self.assertIn(text+'。',Path(exported['path']).read_text(encoding='utf-8'))
        self.assertIn('[local source]',Path(exported['raw_path']).read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
