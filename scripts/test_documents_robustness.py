from test_support import checked_export
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import documents as d

class DocumentRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.job=Path(self.tmp.name);self.out=self.job/'out'
        self.raw=[{'id':'t1','text':'我不喜欢这件事情','start':0,'end':2,'speaker':'说话人1'}]
        self.state={'reviewed_ids':['t1'],'edits':[]}
        for name,data in [('manifest.json',{'coverage_complete':True,'created_at':'2026-11-03','audio_duration':2}),('source.json',{'title':'测试','id':'abc','source_url':'https://example.com'}),('turns.raw.json',self.raw),('turns.corrected.json',self.raw),('review-state.json',self.state)]:d.write_json(self.job/name,data)
    def test_structure_changes_rejected(self):
        for key,value in [('start',1),('end',3),('speaker','其他人')]:
            changed=copy.deepcopy(self.raw);changed[0][key]=value;d.write_json(self.job/'turns.corrected.json',changed)
            with self.subTest(key=key),self.assertRaises(ValueError):checked_export(self.job,self.out)
    def test_failed_or_limited_run_cannot_publish_previously_reviewed_turns(self):
        path=self.job/'manifest.json'
        original=d.read_json(path)
        for change in ({'status':'failed'},{'status':'transcribing'},{'limit_seconds':1.9}):
            with self.subTest(change=change):
                d.write_json(path,{**original,**change})
                with self.assertRaises(ValueError):checked_export(self.job,self.out)
        self.assertFalse(self.out.exists())

    def test_finalize_rejects_changed_audio(self):
        audio=self.job/'audio.wav';audio.write_bytes(b'original')
        path=self.job/'manifest.json';manifest=d.read_json(path)
        manifest['review_input_sha256']=d._digest({'audio':d.file_hash(audio),'turns':self.raw})
        d.write_json(path,manifest)
        audio.write_bytes(b'replacement')
        with self.assertRaisesRegex(ValueError,'音频或原稿已变化'):
            checked_export(self.job,self.out)

    def test_subtitle_blank_lines_do_not_split_a_cue(self):
        self.raw[0]['text']='第一段\n\n第二段'
        d.write_json(self.job/'turns.raw.json',self.raw)
        d.write_json(self.job/'turns.corrected.json',self.raw)
        result=checked_export(self.job,self.out)
        self.assertIn('第一段\n\n第二段',Path(result['path']).read_text(encoding='utf-8'))
        self.assertIn('第一段\n第二段',Path(result['subtitle']).read_text(encoding='utf-8'))
    def test_unlogged_text_change_rejected(self):
        changed=copy.deepcopy(self.raw);changed[0]['text']='我喜欢这件事情';d.write_json(self.job/'turns.corrected.json',changed)
        with self.assertRaises(ValueError):checked_export(self.job,self.out)
    def test_negation_deletion_rejected(self):
        with self.assertRaises(ValueError):d.apply_review(self.raw,{'reviewed_ids':['t1'],'edits':[{'turn_id':'t1','before':self.raw[0]['text'],'after':'我喜欢这件事情','reason':'错字','evidence':'上下文'}]})
    def test_punctuation_only_rejected(self):
        for text in ['   ','！！！','😀😀😀']:
            changed=copy.deepcopy(self.raw);changed[0]['text']=text;d.write_json(self.job/'turns.corrected.json',changed)
            with self.subTest(text=text),self.assertRaises(ValueError):checked_export(self.job,self.out)
    def test_missing_or_corrupt_companions_repaired(self):
        for field in ['raw_path','correction_log','subtitle']:
            result=checked_export(self.job,self.out);path=Path(result[field]);expected=path.read_bytes();path.unlink()
            recovered=checked_export(self.job,self.out)
            self.assertEqual(Path(recovered[field]).read_bytes(),expected)
            Path(recovered[field]).write_text('损坏',encoding='utf-8')
            recovered=checked_export(self.job,self.out)
            self.assertEqual(Path(recovered[field]).read_bytes(),expected)
    def test_metadata_invalidates_cache(self):
        first=checked_export(self.job,self.out)
        source=d.read_json(self.job/'source.json');source['source_url']='https://changed.example';d.write_json(self.job/'source.json',source)
        second=checked_export(self.job,self.out)
        self.assertIn('https://changed.example',Path(second['path']).read_text(encoding='utf-8'))
        manifest=d.read_json(self.job/'manifest.json');manifest['speaker_report']={'count':9};d.write_json(self.job/'manifest.json',manifest)
        third=checked_export(self.job,self.out)
        self.assertIn('speaker_count: 9',Path(third['path']).read_text(encoding='utf-8'))
    def test_partial_export_recovers_without_duplicate(self):
        original=d.write_json
        def fail_manifest(path,data):
            if Path(path).name=='manifest.json':raise OSError('simulated interruption')
            return original(path,data)
        with patch.object(d,'write_json',side_effect=fail_manifest),self.assertRaises(OSError):checked_export(self.job,self.out)
        result=checked_export(self.job,self.out)
        self.assertTrue(result['verified']);self.assertEqual(len(list(self.out.glob('*.srt'))),1)
    def test_review_transaction_recovers(self):
        changed,edits,_=d.apply_review(self.raw,{'reviewed_ids':['t1'],'edits':[{'turn_id':'t1','before':self.raw[0]['text'],'after':self.raw[0]['text']+'。','reason':'标点','evidence':'句末'}]})
        state=copy.deepcopy(self.state);state['edits']=edits
        original=d.write_json
        def fail_state(path,data):
            if Path(path).name=='review-state.json':raise OSError('simulated interruption')
            return original(path,data)
        with patch.object(d,'write_json',side_effect=fail_state),self.assertRaises(OSError):d.save_review(self.job,changed,state)
        d.recover_review(self.job)
        self.assertEqual(d.read_json(self.job/'turns.corrected.json'),changed)
        self.assertEqual(d.read_json(self.job/'review-state.json'),state)
        checked_export(self.job,self.out)

    def test_each_export_file_interruption_recovers(self):
        original=d._write_text
        for fail_index in range(1,5):
            out=self.out/str(fail_index);calls=0
            def interrupt(path,text):
                nonlocal calls
                if Path(path).parent==out.resolve():
                    calls+=1
                    if calls==fail_index:raise OSError('interrupted file write')
                return original(path,text)
            with self.subTest(file=fail_index):
                with patch.object(d,'_write_text',side_effect=interrupt),self.assertRaises(OSError):checked_export(self.job,out)
                result=checked_export(self.job,out)
                self.assertTrue(result['verified'])
                self.assertEqual(len(list(out.iterdir())),4)
                self.assertEqual(checked_export(self.job,out)['status'],'existing')

    def test_damaged_export_journal_fails_before_publishing(self):
        original=d._write_text
        def interrupt(path,text):
            if Path(path).parent==self.out.resolve():raise OSError('interruption')
            return original(path,text)
        with patch.object(d,'_write_text',side_effect=interrupt),self.assertRaises(OSError):checked_export(self.job,self.out)
        journal=self.job/'export-transaction.json';data=d.read_json(journal)
        data['payload']['files'][0]['text']='damaged';d.write_json(journal,data)
        with self.assertRaises(ValueError):checked_export(self.job,self.out)
        self.assertEqual(list(self.out.iterdir()),[])

    def test_duplicate_invalid_timing_and_speaker_rejected(self):
        bad=[]
        bad.append(self.raw+self.raw)
        for key,value in [('start',float('nan')),('end',-1),('start',True),('speaker','')]:
            turns=copy.deepcopy(self.raw);turns[0][key]=value;bad.append(turns)
        for turns in bad:
            with self.subTest(turns=turns),self.assertRaises(ValueError):d.apply_review(turns,{'reviewed_ids':['t1'],'edits':[]})

    def test_cumulative_audit_replays_and_forged_audit_fails(self):
        current=copy.deepcopy(self.raw);state=copy.deepcopy(self.state)
        for punctuation in ['。','！']:
            edit={'turn_id':'t1','before':current[0]['text'],'after':self.raw[0]['text']+punctuation,'reason':'punctuation','evidence':'sentence ending'}
            current,edits,_=d.apply_review(current,{'reviewed_ids':['t1'],'edits':[edit]})
            state['edits'].extend(edits);d.save_review(self.job,current,state)
        checked_export(self.job,self.out)
        state['edits'][0]['before']='forged';d.write_json(self.job/'review-state.json',state)
        with self.assertRaises(ValueError):checked_export(self.job,self.out)

    def test_raw_metadata_and_audit_metadata_invalidate_cache(self):
        checked_export(self.job,self.out)
        raw=copy.deepcopy(self.raw);raw[0]['text']+='。'
        d.write_json(self.job/'turns.raw.json',raw);d.write_json(self.job/'turns.corrected.json',raw)
        second=checked_export(self.job,self.out);self.assertEqual(second['status'],'complete')
        state=copy.deepcopy(self.state);state['reviewer']='different reviewer';d.write_json(self.job/'review-state.json',state)
        third=checked_export(self.job,self.out);self.assertEqual(third['status'],'complete')

    def test_legacy_negation_error_and_restoration_replay(self):
        original=self.raw[0]['text'];intermediate=original.replace('不','')
        state=copy.deepcopy(self.state)
        state['edits']=[{'turn_id':'t1','before':original,'after':intermediate,'reason':'legacy typo correction','evidence':'old review'},
                        {'turn_id':'t1','before':intermediate,'after':original,'reason':'restore negation','evidence':'conservative restoration'}]
        d.write_json(self.job/'review-state.json',state)
        self.assertTrue(checked_export(self.job,self.out)['verified'])

    def test_reset_recovers_each_triple_write_and_preserves_previous_bytes(self):
        for index,name in enumerate(['turns.raw.json','turns.corrected.json','review-state.json']):
            with self.subTest(name=name):
                before={n:(self.job/n).read_bytes() for n in ['turns.raw.json','turns.corrected.json','review-state.json']}
                turns=copy.deepcopy(self.raw);turns[0]['text']+='x'*(index+1)
                original=d.write_json
                def interrupt(path,data):
                    if Path(path).name==name:raise OSError('reset interrupted')
                    return original(path,data)
                with patch.object(d,'write_json',side_effect=interrupt),self.assertRaises(OSError):d.reset_review(self.job,turns)
                journal=d._read_transaction(self.job/'review-reset-transaction.json')
                d.recover_review(self.job)
                self.assertEqual(d.read_json(self.job/'turns.raw.json'),turns)
                self.assertEqual(d.read_json(self.job/'turns.corrected.json'),turns)
                self.assertEqual(d.read_json(self.job/'review-state.json')['reviewed_ids'],[])
                backup=d.read_json(self.job/journal['backup_name'])
                self.assertEqual({n:bytes.fromhex(v) for n,v in backup['files'].items()},before)

    def test_reset_recovers_pending_edits_before_preserving_history(self):
        current,edits,_=d.apply_review(self.raw,{'reviewed_ids':['t1'],'edits':[{'turn_id':'t1','before':self.raw[0]['text'],'after':self.raw[0]['text']+'。','reason':'punctuation','evidence':'sentence ending'}]})
        state={'reviewed_ids':['t1'],'edits':edits}
        d._write_transaction(self.job/'review-transaction.json',{'turns':current,'state':state})
        d.reset_review(self.job,self.raw)
        backup=d.read_json(next(self.job.glob('review-reset-backup-*.json')))
        self.assertEqual(bytes.fromhex(backup['files']['review-state.json']), (d.json.dumps(state,ensure_ascii=False,indent=2)+'\n').encode())
        self.assertFalse((self.job/'review-transaction.json').exists())

    def test_invalid_pending_review_prevents_reset(self):
        d.write_json(self.job/'review-transaction.json',{'payload':{},'sha256':'damaged'})
        before=(self.job/'turns.raw.json').read_bytes()
        with self.assertRaises(ValueError):d.reset_review(self.job,self.raw)
        self.assertEqual((self.job/'turns.raw.json').read_bytes(),before)
        self.assertFalse((self.job/'review-reset-transaction.json').exists())

    def test_reset_preserves_malformed_old_pair_and_recovers_backup_failure(self):
        (self.job/'review-state.json').write_bytes(b'invalid-json')
        original=d.write_json
        def interrupt(path,data):
            if Path(path).name.startswith('review-reset-backup-'):raise OSError('backup interrupted')
            return original(path,data)
        with patch.object(d,'write_json',side_effect=interrupt),self.assertRaises(OSError):d.reset_review(self.job,self.raw)
        self.assertEqual((self.job/'review-state.json').read_bytes(),b'invalid-json')
        d.recover_review(self.job)
        backup=d.read_json(next(self.job.glob('review-reset-backup-*.json')))
        self.assertEqual(bytes.fromhex(backup['files']['review-state.json']),b'invalid-json')

if __name__=='__main__':unittest.main()
