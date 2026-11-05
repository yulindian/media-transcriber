import unittest
import tempfile
import json
from pathlib import Path
import numpy as np
import sources
import documents
import engine


class PipelineTests(unittest.TestCase):
    def test_short_reply_can_receive_punctuation(self):
        turns=[{'id':'t1','text':'是','start':0,'end':1,'speaker':'说话人1'}]
        changed,_,_=documents.apply_review(turns,{'reviewed_ids':['t1'],'edits':[{'turn_id':'t1','before':'是','after':'是。','reason':'补标点','evidence':'完整短应答'}]})
        self.assertEqual(changed[0]['text'],'是。')
    def test_xhs_login_redirect_preserves_signature(self):
        u = sources.normalize_xhs('https://www.xiaohongshu.com/login?redirectPath=https%3A%2F%2Fwww.xiaohongshu.com%2Fdiscovery%2Fitem%2Fabcdef123%3Fxsec_token%3Da%252Bb%253D%26xsec_source%3Dapp_share')
        self.assertIn('/explore/abcdef123?', u)
        self.assertIn('xsec_token=a%2Bb%3D', u)

    def test_unknown_speaker_count_is_not_forced_two(self):
        rng = np.random.default_rng(7)
        x = np.array([1., 0., 0.]) + rng.normal(0, .02, (60, 3))
        labels, report = engine.cluster_speakers(x, np.ones(60)*4, None)
        self.assertEqual(len(set(labels)), 1)
        self.assertEqual(report['count_mode'], 'estimated')

    def test_two_voice_clusters_survive_short_outliers(self):
        rng = np.random.default_rng(3)
        x = np.r_[np.array([1.,0.,0.])+rng.normal(0,.03,(30,3)), np.array([0.,1.,0.])+rng.normal(0,.03,(30,3)), [[0,0,1],[-1,0,0]]]
        labels, report = engine.cluster_speakers(x, np.r_[np.ones(60)*4,[.3,.2]], 2)
        self.assertEqual(len(set(labels[:30])), 1)
        self.assertEqual(len(set(labels[30:60])), 1)
        self.assertNotEqual(labels[0], labels[30])

    def test_short_two_speaker_sample_is_not_defaulted_single(self):
        x=np.array([[1,.02,0],[0,1,0],[1,-.02,0],[.02,1,0],[1,0,.02]],dtype=float)
        labels,report=engine.cluster_speakers(x,np.array([3,3,18,8,15]),None)
        self.assertEqual(report['count'],2)

    def test_review_cannot_drop_turns_or_apply_stale_edits(self):
        turns=[{'id':'t1','text':'我是肥姐','start':0,'end':2,'speaker':'说话人1'}]
        with self.assertRaises(ValueError):
            documents.apply_review(turns, {'reviewed_ids':['t1'],'edits':[{'turn_id':'t1','before':'其他内容','after':'我是肥杰','reason':'姓名','evidence':'标题'}]})
        corrected, edits, reviewed = documents.apply_review(turns, {'reviewed_ids':['t1'],'edits':[{'turn_id':'t1','before':'我是肥姐','after':'我是肥杰','reason':'姓名误识别','evidence':'节目页作者名为肥杰'}]})
        self.assertEqual(corrected[0]['text'], '我是肥杰')
        self.assertEqual(turns[0]['text'], '我是肥姐')
        self.assertEqual(reviewed, ['t1'])

    def test_partial_and_unreviewed_are_not_complete_final(self):
        with self.assertRaises(ValueError):
            documents.validate_final({'coverage_complete':True}, [{'id':'t1'}], [])
        with self.assertRaises(ValueError):
            documents.validate_final({'coverage_complete':False}, [{'id':'t1'}], ['t1'])

    def test_redaction_of_download_error_tokens(self):
        out=sources.redact('failed https://x.test/?xsec_token=secret&sign=private Cookie: abc')
        self.assertNotIn('secret',out)
        self.assertNotIn('private',out)

    def test_changed_transcript_does_not_reuse_old_final(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);out=p/'out';out.mkdir();old=out/'old.md';old.write_text('旧成稿',encoding='utf-8')
            raw=[{'id':'t1','text':'新的正文','start':0,'end':2,'speaker':'说话人1'}]
            state={'reviewed_ids':['t1'],'edits':[]}
            documents.write_json(p/'review-state.json',state)
            m={'coverage_complete':True,'created_at':'2026-11-03','audio_duration':2,'final_path':str(old),'final_sha256':documents.file_hash(old),'review_sha256':documents.file_hash(p/'review-state.json'),'final_turns_sha256':'different-old-input'}
            documents.write_json(p/'manifest.json',m)
            documents.write_json(p/'source.json',{'id':'abc','title':'测试','source_url':'https://example.com','platform':'测试'})
            documents.write_json(p/'turns.raw.json',raw);documents.write_json(p/'turns.corrected.json',raw)
            result=documents.finalize(p,out)
            self.assertNotEqual(result['path'],str(old))
            self.assertIn('新的正文',Path(result['path']).read_text(encoding='utf-8'))

    def test_combine_rejects_damaged_cached_token_counts(self):
        bad={'chunks':[{'start':0,'end':5,'text':'完整正文','tokens':list('完整正文'),'timestamps':[1]}]}
        with self.assertRaises(ValueError):
            engine.combine(bad,{'segments':[]})

    def test_changed_output_exports_to_requested_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            turn=[{'id':'t1','text':'正文','start':0,'end':2,'speaker':'说话人1'}]
            documents.write_json(p/'manifest.json',{'coverage_complete':True,'created_at':'2026-11-03','audio_duration':2})
            documents.write_json(p/'source.json',{'id':'abc','title':'测试','source_url':'https://example.com','platform':'测试'})
            documents.write_json(p/'turns.raw.json',turn);documents.write_json(p/'turns.corrected.json',turn)
            documents.write_json(p/'review-state.json',{'reviewed_ids':['t1'],'edits':[]})
            documents.finalize(p,p/'one')
            result=documents.finalize(p,p/'two')
            self.assertEqual(Path(result['path']).parent,p/'two')


if __name__ == '__main__':
    unittest.main()
