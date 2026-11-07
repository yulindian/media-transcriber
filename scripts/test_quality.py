import copy
import tempfile
import unittest
import wave
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import documents as d
import quality
import transcribe


class QualityTests(unittest.TestCase):
    def turns(self):
        return [{'id':'t1','start':1.,'end':2.,'speaker':'说话人1','text':'没有问题'},
                {'id':'t2','start':2.,'end':2.5,'speaker':'说话人2（待核对）','text':'嗯'}]

    def test_preflight_collects_errors_and_leaves_inputs_unchanged(self):
        turns=self.turns();before=copy.deepcopy(turns)
        review={'reviewed_ids':['t1','t2'],'edits':[
            dict(turn_id='t1',before='过期文本',after='新文本',reason='test',evidence='test'),
            dict(turn_id='t2',before='嗯',after='不嗯',reason='test',evidence='test')]}
        report=d.preflight_review(turns,review)
        self.assertFalse(report['valid'])
        self.assertEqual([e['turn_id'] for e in report['errors']],['t1','t2'])
        self.assertEqual(turns,before)

    def test_paragraphs_preserve_every_character_and_do_not_merge_overlap(self):
        t=self.turns()[0];t['text']=('这是一个完整句子。'*40)
        rendered=d.body([t],True).split('：',1)[1].rstrip('\n')
        self.assertEqual(rendered.replace('\n\n',''),t['text'])
        self.assertIn('\n\n',rendered)
        a,b=self.turns();b.update(speaker=a['speaker'],start=1.5,text='继续')
        self.assertEqual(len(d._reading_turns([a,b])),2)

    def test_real_pcm_clip_bounds_and_read_only_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            job=Path(tmp);turns=self.turns()
            for name,data in [('turns.raw.json',turns),('turns.corrected.json',turns),
                              ('manifest.json',{'audio_duration':4}),
                              ('review-state.json',{'reviewed_ids':['t1'],'edits':[]})]:
                d.write_json(job/name,data)
            with wave.open(str(job/'audio.wav'),'wb') as f:
                f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\0\0'*64000)
            original={p.name:p.read_bytes() for p in job.glob('*.json')}
            payload=job/'inputs'/'review.json'
            d.write_json(payload,{'reviewed_ids':['t2'],'edits':[]})
            result=transcribe.edit(SimpleNamespace(job=str(job),input=str(payload),check=True))
            self.assertTrue(result['valid'])
            self.assertEqual(original,{p.name:p.read_bytes() for p in job.glob('*.json')})
            report=quality.inspect(job)
            self.assertFalse(report['text_review_complete'])
            self.assertEqual(report['counts']['speaker_uncertain'],1)
            result=quality.clip(job,'t1',.5)
            self.assertEqual((result['start'],result['end']),(.5,2.5))
            with wave.open(result['path']) as f:self.assertEqual(f.getnframes(),32000)
            self.assertEqual(original,{p.name:p.read_bytes() for p in job.glob('*.json')})
            with patch('engine.asr',return_value={'chunks':[{'text':'候选上下文'}]}) as recognize:
                result=quality.recheck(job,'t1',{'runtime':tmp,'asr_language':'auto'},.5)
                self.assertEqual(result['candidate_text'],'候选上下文')
                self.assertEqual(recognize.call_args.kwargs['language'],'auto')
                self.assertEqual(original,{p.name:p.read_bytes() for p in job.glob('*.json')})
            with self.assertRaises(ValueError):quality.clip(job,'../escape')
            with self.assertRaises(ValueError):quality.clip(job,'t1',float('nan'))


if __name__=='__main__':unittest.main()
