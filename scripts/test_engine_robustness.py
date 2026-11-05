"""Offline regression tests; no model downloads or long ASR inference."""
import json
import random
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import engine
import documents


def chunk(start=0, end=3, text='字', tokens=None, timestamps=None):
    return dict(start=start,end=end,text=text,tokens=tokens if tokens is not None else [text],
                timestamps=timestamps if timestamps is not None else [start+.1])


def legacy_label(t, regions):
    active=[s for s in regions if s['start']<=t<=s['end']]
    if not active and regions:
        near=min(regions,key=lambda s:min(abs(t-s['start']),abs(t-s['end'])))
        if min(abs(t-near['start']),abs(t-near['end']))<=.6:active=[near]
    if not active:return '说话人未确定'
    value='/'.join('说话人'+str(k+1) for k in sorted({s['speaker'] for s in active}))
    if any(s.get('margin',0)<.1 for s in active):value+='（待核对）'
    return value


class EngineRobustness(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.job=Path(self.tmp.name)
        with wave.open(str(self.job/'audio.wav'),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\0\0'*48000)

    def test_header_duration_without_audio_decode(self):
        self.assertEqual(engine.audio_duration(self.job/'audio.wav'),3)

    def test_single_speaker_never_loads_waveform(self):
        with patch.object(engine,'audio',side_effect=AssertionError('waveform loaded')):
            result=engine.diarize(self.job,self.job,count=1)
        self.assertEqual(result['segments'][0]['end'],3)

    def test_warm_diarize_does_not_load_waveform_and_keeps_signature(self):
        engine.write_json(self.job/'speaker-regions.json',{'signature':'old','segments':[{'start':0,'end':3}]})
        np.save(self.job/'speaker-features.npy',np.array([[1.,0.]]))
        with patch.object(engine,'signature',return_value='old') as sig, patch.object(engine,'file_hash',return_value='hash'), patch.object(engine,'audio',side_effect=AssertionError('waveform loaded')):
            result=engine.diarize(self.job,self.job)
        self.assertEqual(result['report']['count'],1)
        self.assertEqual(sig.call_args.args[1],'segmentation-v1')

    def test_warm_asr_rejects_corrupt_cached_chunks(self):
        for bad in [{'duration':3,'chunks':[chunk(tokens='字')]}, {'duration':3,'chunks':[chunk(end=1)]}, {'duration':3,'chunks':[chunk(timestamps=[True])]}, {'duration':3,'chunks':[chunk(timestamps=[float('nan')])]}]:
            with self.subTest(bad=bad):
                engine.write_json(self.job/'asr.json',dict(signature='old',**bad))
                with patch.object(engine,'signature',return_value='old'),patch.object(engine,'file_hash',return_value='hash'),self.assertRaises(ValueError):
                    engine.asr(self.job,self.job)

    def test_valid_old_asr_cache_does_not_initialize_recognizer(self):
        cached={'signature':'old','duration':3,'chunks':[chunk()]}
        engine.write_json(self.job/'asr.json',cached)
        with patch.object(engine,'signature',return_value='old'),patch.object(engine,'file_hash',return_value='hash'),patch.object(engine,'audio',side_effect=AssertionError('decoded')):
            self.assertEqual(engine.asr(self.job,self.job),cached)

    def test_checkpoint_recovers_only_verified_prefix(self):
        valid=chunk(end=1)
        cases=[chunk(start=1,end=2,tokens='字'),chunk(start=1,end=1),chunk(start=1,end=2,timestamps=[float('nan')]),chunk(start=1,end=2,tokens=['另']),chunk(start=1,end=2,timestamps=[True]),{'start':1},'torn-json']
        for bad in cases:
            with self.subTest(bad=bad):
                engine.write_json(self.job/'asr.signature.json',{'signature':'sig'})
                (self.job/'asr.json').unlink(missing_ok=True)
                (self.job/'asr.chunks.jsonl').write_text(json.dumps(valid)+'\n'+(bad if isinstance(bad,str) else json.dumps(bad))+'\n'+json.dumps(chunk(start=2,end=3))+'\n',encoding='utf-8')
                waveform_lengths=[]
                stream=SimpleNamespace(result=SimpleNamespace(tokens=['新'],timestamps=[.1],text='新'),accept_waveform=lambda rate,a:waveform_lengths.append(len(a)))
                rec=SimpleNamespace(create_stream=lambda:stream,decode_stream=lambda stream:None)
                with patch.object(engine,'signature',return_value='sig'),patch.object(engine,'file_hash',return_value='hash'),patch.object(engine.sherpa_onnx.OfflineRecognizer,'from_sense_voice',return_value=rec),patch.object(engine.subprocess,'run',return_value=SimpleNamespace(stderr='')):
                    result=engine.asr(self.job,self.job)
                self.assertEqual(result['chunks'][0],valid)
                self.assertEqual(result['chunks'][1]['start'],1)
                self.assertEqual(waveform_lengths,[32000])
                self.assertEqual(''.join(c['text'] for c in result['chunks']),'字新')

    def test_invalid_feature_shapes_and_durations_are_rejected(self):
        for features,durations in [([1,2],[1,2]),(np.ones((2,2,1)),[1,2]),([[1,0],[0,1]],[1]),([[1,0]],[[1]]),([[0,0]],[1]),([[1,0]],[0]),([[1,0]],[float('inf')]),([[float('nan'),0]],[1])]:
            with self.subTest(features=features,durations=durations),self.assertRaises(ValueError):
                engine.cluster_speakers(features,durations,None)

    def test_mismatched_region_feature_cache_rejected(self):
        engine.write_json(self.job/'speaker-regions.json',{'signature':'sig','segments':[{'start':0,'end':1},{'start':1,'end':3}]})
        np.save(self.job/'speaker-features.npy',np.array([[1.,0.]]))
        with patch.object(engine,'signature',return_value='sig'),patch.object(engine,'file_hash',return_value='hash'),self.assertRaises(ValueError):
            engine.diarize(self.job,self.job)

    def test_invalid_regions_are_rejected(self):
        for region in [{'start':1,'end':0,'speaker':0},{'start':-1,'end':2,'speaker':0},{'start':float('nan'),'end':2,'speaker':0},{'start':0,'end':2,'speaker':True},{'start':0,'end':2,'speaker':0,'margin':float('nan')}]:
            with self.subTest(region=region),self.assertRaises(ValueError):
                engine.combine({'chunks':[chunk()]},{'segments':[region]})

    def test_cached_regions_cannot_exceed_audio(self):
        engine.write_json(self.job/'speaker-regions.json',{'signature':'sig','segments':[{'start':0,'end':4}]})
        np.save(self.job/'speaker-features.npy',np.array([[1.,0.]]))
        with patch.object(engine,'signature',return_value='sig'),patch.object(engine,'file_hash',return_value='hash'),self.assertRaises(ValueError):
            engine.diarize(self.job,self.job)

    def test_invalid_count_rejected_before_loading_models(self):
        for count in (True,0,-1,2.5,'2'):
            with self.subTest(count=count),self.assertRaises(ValueError):engine.diarize(self.job,self.job,count=count)

    def test_unknown_count_can_estimate_three_speakers(self):
        features=np.repeat(np.eye(3),8,axis=0)
        labels,report=engine.cluster_speakers(features,np.ones(24)*3,None)
        self.assertEqual(report['count'],3);self.assertEqual(len(set(labels)),3)

    def test_randomized_interval_labels_equal_legacy(self):
        rng=random.Random(193)
        regions=[{'start':rng.uniform(0,8),'end':0,'speaker':i%3,'margin':.05 if i%4==0 else .8} for i in range(25)]
        for s in regions:s['end']=s['start']+rng.uniform(.05,1)
        rng.shuffle(regions)
        times=[rng.uniform(0,10) for _ in range(200)]+[t for s in regions for t in (s['start'],s['end'])]
        for t in times:
            result=engine.combine({'chunks':[chunk(end=10,timestamps=[t])]},{'segments':regions})
            self.assertEqual(result[0]['speaker'],legacy_label(t,regions))

    def test_nearest_tie_uses_original_region_order(self):
        for regions in ([{'start':2,'end':3,'speaker':1},{'start':0,'end':1,'speaker':0}], [{'start':0,'end':1,'speaker':0},{'start':2,'end':3,'speaker':1}]):
            out=engine.combine({'chunks':[chunk(timestamps=[1.5])]},{'segments':regions})
            self.assertEqual(out[0]['speaker'],legacy_label(1.5,regions))

    def test_cross_chunk_timestamp_overlap_preserves_word_order(self):
        chunks=[chunk(end=1,text='甲',timestamps=[1.3]),chunk(start=1,end=3,text='乙',timestamps=[1.1])]
        out=engine.combine({'chunks':chunks},{'segments':[{'start':0,'end':1.2,'speaker':0},{'start':1.2,'end':3,'speaker':1}]})
        self.assertEqual(''.join(t['text'] for t in out),'甲乙')
        self.assertEqual([t['speaker'] for t in out],['说话人2（待核对）','说话人1（待核对）'])
        documents._validate_turns(out)

    def test_interrupted_checkpoint_switch_leaves_old_signature(self):
        engine.write_json(self.job/'asr.signature.json',{'signature':'old'})
        (self.job/'asr.chunks.jsonl').write_text(json.dumps(chunk()),encoding='utf-8')
        with patch.object(engine,'signature',return_value='new'), \
                patch.object(engine,'file_hash',return_value='hash'), \
                patch.object(engine.sherpa_onnx.OfflineRecognizer,'from_sense_voice'), \
                patch.object(engine.subprocess,'run',return_value=SimpleNamespace(stderr='')), \
                patch.object(engine,'_write_text',side_effect=OSError('interrupted')), \
                self.assertRaises(OSError):
            engine.asr(self.job,self.job)
        self.assertEqual(engine.read_json(self.job/'asr.signature.json')['signature'],'old')

    def test_bad_chunk_structure_rejected_by_combine(self):
        cases=[chunk(tokens='字'),chunk(tokens=[1]),chunk(timestamps='x'),chunk(timestamps=[True]),chunk(timestamps=[float('inf')]),chunk(end=0),chunk(text='另',tokens=['字']),chunk(text='字字',tokens=['字','字'],timestamps=[2,1])]
        for bad in cases:
            with self.subTest(bad=bad),self.assertRaises(ValueError):engine.combine({'chunks':[bad]},{'segments':[]})


if __name__=='__main__':unittest.main()
