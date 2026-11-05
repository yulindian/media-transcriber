import argparse
import json
import tempfile
import unittest
import subprocess
import sys
import wave
from unittest import mock
from pathlib import Path

import documents
import transcribe


class OrchestrationTests(unittest.TestCase):
    def fixture(self, root):
        turns=[{'id':f't{i}','text':'原文'+str(i)+'很长'*30,'start':i,'end':i+1,'speaker':'说话人1'} for i in range(6)]
        documents.write_json(root/'turns.raw.json',turns)
        documents.write_json(root/'turns.corrected.json',turns)
        documents.write_json(root/'review-state.json',{'reviewed_ids':['t0','t2'],'edits':[]})
        documents.write_json(root/'source.json',{'id':'a','title':'标题','author':'作者','source_url':'https://example.com','media_url':'https://example.com/?token=private','description':'简介'})
        documents.write_json(root/'manifest.json',{'status':'review_pending','audio_duration':6,'speaker_report':{'count':1},'output_dir':str(root/'requested')})

    def test_review_next_avoids_already_reviewed_and_bounds_characters(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p)
            args=argparse.Namespace(job=str(p),offset=0,count=30,max_chars=150,next=True,verbose=False)
            result=transcribe.review(args)
            self.assertTrue(result['turns'])
            self.assertNotIn('t0',[x['id'] for x in result['turns']])
            self.assertNotIn('t2',[x['id'] for x in result['turns']])
            self.assertLessEqual(sum(len(x['text']) for x in result['turns']),150)
            self.assertNotIn('media_url',result['source'])

    def test_output_remembers_run_request_unless_overridden(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p)
            self.assertEqual(transcribe.output_for_job(p,None,{'output':'default'}),str(p/'requested'))
            self.assertEqual(transcribe.output_for_job(p,'override',{'output':'default'}),'override')

    def test_same_job_lock_prevents_parallel_writers_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            with transcribe.job_lock(p):
                with self.assertRaises(ValueError):
                    with transcribe.job_lock(p):pass
            with transcribe.job_lock(p):pass

    def test_review_never_truncates_one_large_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p)
            args=argparse.Namespace(job=str(p),offset=0,count=30,max_chars=10,next=False,verbose=False)
            result=transcribe.review(args)
            self.assertEqual(len(result['turns']),1)
            self.assertEqual(result['turns'][0]['text'],documents.read_json(p/'turns.corrected.json')[0]['text'])

    def test_lock_is_released_after_worker_termination(self):
        with tempfile.TemporaryDirectory() as temp:
            code="import sys,os; from pathlib import Path; import transcribe;\nwith transcribe.job_lock(Path(sys.argv[1])):\n print('LOCKED',flush=True);sys.stdin.readline();os._exit(17)"
            kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
            process=subprocess.Popen([sys.executable,'-c',code,temp],cwd=Path(__file__).parent,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,**kwargs)
            try:
                self.assertEqual(process.stdout.readline().strip(),'LOCKED')
                with self.assertRaises(ValueError):
                    with transcribe.job_lock(Path(temp)):pass
                process.stdin.write('\n');process.stdin.flush();process.wait(timeout=10)
                with transcribe.job_lock(Path(temp)):pass
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=10)
                process.stdin.close();process.stdout.close();process.stderr.close()

    def test_speaker_failure_is_explicit_and_header_read_does_not_decode_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);source=p/'input.wav';job=p/'job'
            with wave.open(str(source),'wb') as f:
                f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\x10\0'*32000)
            args=argparse.Namespace(input=str(source),limit_seconds=None,job=str(job),output=str(p/'requested'),cookies=None,source_url=None,title=None,speakers='auto',speaker_evidence='')
            fake={'chunks':[{'start':0,'end':2,'text':'测试','tokens':['测','试'],'timestamps':[.1,.5]}],'elapsed_this_run':0}
            with mock.patch.object(transcribe.engine,'asr',return_value=fake),mock.patch.object(transcribe.engine,'diarize',side_effect=RuntimeError('speaker unavailable')),mock.patch.object(transcribe.engine,'audio',side_effect=AssertionError('header must not decode waveform')):
                result=transcribe.run(args,{'runtime':str(p),'output':'default','threads':1})
            self.assertTrue(result['warnings'])
            self.assertEqual(result['speaker_report']['count_mode'],'unavailable')
            self.assertEqual(documents.read_json(job/'turns.raw.json')[0]['speaker'],'说话人未确定')
            self.assertEqual(transcribe.output_for_job(job,None,{'output':'default'}),str(p/'requested'))

    def test_exact_silence_is_rejected_before_model_hallucination(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);source=p/'silence.wav'
            with wave.open(str(source),'wb') as f:
                f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\0\0'*48000)
            args=argparse.Namespace(input=str(source),limit_seconds=None,job=str(p/'job'),output=None,cookies=None,source_url=None,title=None,speakers='auto',speaker_evidence='')
            fake={'chunks':[{'start':0,'end':3,'text':'的','tokens':['的'],'timestamps':[.5]}],'elapsed_this_run':0}
            with mock.patch.object(transcribe.engine,'asr',return_value=fake) as decoder,mock.patch.object(transcribe.engine,'diarize',return_value={'segments':[],'report':{'count':0,'count_mode':'unavailable'}}):
                with self.assertRaisesRegex(ValueError,'静音'):
                    transcribe.run(args,{'runtime':str(p),'output':'default'})
                decoder.assert_not_called()

    def test_operational_timing_changes_do_not_duplicate_final_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p)
            turns=documents.read_json(p/'turns.raw.json')
            documents.write_json(p/'review-state.json',{'reviewed_ids':[t['id'] for t in turns],'edits':[]})
            m=documents.read_json(p/'manifest.json');m.update(coverage_complete=True,created_at='2026-11-03',elapsed_last_run=7)
            documents.write_json(p/'manifest.json',m)
            first=documents.finalize(p,p/'out')
            m=documents.read_json(p/'manifest.json');m['elapsed_last_run']=.5;m['asr_elapsed']=0
            documents.write_json(p/'manifest.json',m)
            second=documents.finalize(p,p/'out')
            self.assertEqual(second['status'],'existing')
            self.assertEqual(first['path'],second['path'])

    def test_finalize_locks_old_journal_directory_and_new_requested_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);job=p/'job';job.mkdir();old=p/'old';new=p/'new'
            documents.write_json(job/'manifest.json',{'output_dir':str(new)})
            documents._write_transaction(job/'export-transaction.json',{'files':[{'path':str(old/'old.md'),'text':'old'}],'manifest_update':{}})
            args=argparse.Namespace(job=str(job),output=None)
            def finalize_probe(*unused):
                for output in (old,new):
                    with self.assertRaises(ValueError):
                        with transcribe.job_lock(output):pass
                return {'status':'probe'}
            with mock.patch.object(documents,'finalize',side_effect=finalize_probe):
                self.assertEqual(transcribe.finalize_job(args,{'output':'default'})['status'],'probe')


if __name__=='__main__':unittest.main()
