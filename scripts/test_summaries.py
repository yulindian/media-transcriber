"""Summary ranges and export consistency, using synthetic transcripts only."""
import tempfile
import unittest
from pathlib import Path

import documents as d
import summaries
import quality
from test_support import checked_export


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.job=Path(self.tmp.name)
        self.turns=[dict(id='a',text='先定义目标。',start=3,end=12,speaker='说话人1'),
                    dict(id='b',text='接着讨论方案。',start=12,end=25,speaker='说话人1'),
                    dict(id='c',text='最后介绍后续安排。',start=25,end=40,speaker='说话人1')]
        for name,data in [('manifest.json',dict(created_at='2026-11-09',coverage_complete=True,audio_duration=42,speaker_report={'count':1})),
                          ('source.json',dict(id='synthetic',title='合成示例',source_url='https://example.com/demo')),
                          ('turns.raw.json',self.turns),('turns.corrected.json',self.turns),
                          ('review-state.json',dict(reviewed_ids=['a','b','c'],edits=[]))]:
            d.write_json(self.job/name,data)

    def timeline(self):
        return dict(mode='timeline',chapters=[
            dict(start_turn_id='a',end_turn_id='b',title='目标与方案',summary='先明确目标，再讨论可选方案。'),
            dict(start_turn_id='c',end_turn_id='c',title='后续安排',summary='介绍下一步的安排。')])

    def test_overview_export_is_one_summary_and_body_stays_plain(self):
        summaries.save(self.job,dict(mode='overview',summary='围绕目标说明方案与后续安排。'))
        result=checked_export(self.job,self.job/'out')
        text=Path(result['path']).read_text(encoding='utf-8')
        self.assertIn('## 内容总结\n\n围绕目标说明方案与后续安排。\n\n## 正文',text)
        self.assertNotIn('## 时间轴总结',text)
        self.assertNotIn('内容总结',Path(result['raw_path']).read_text(encoding='utf-8'))
        self.assertEqual(checked_export(self.job,self.job/'out')['status'],'existing')

    def test_timeline_uses_actual_turn_times_and_is_separate_from_body(self):
        summaries.save(self.job,self.timeline())
        text=Path(checked_export(self.job,self.job/'out')['path']).read_text(encoding='utf-8')
        self.assertIn('### 00:00:03–00:00:25｜目标与方案',text)
        self.assertIn('### 00:00:25–00:00:40｜后续安排',text)
        self.assertNotIn('00:00:',text.split('## 正文\n\n')[1])

    def test_bad_ranges_and_mode_fail_without_overwriting_summary(self):
        good=dict(mode='overview',summary='先明确目标，再讨论方案。');summaries.save(self.job,good)
        before=(self.job/'summary.json').read_bytes()
        invalid=[]
        for change in [('start_turn_id','missing'),('start_turn_id','b'),('end_turn_id','c')]:
            payload=self.timeline();payload['chapters'][0][change[0]]=change[1];invalid.append(payload)
        invalid.extend([dict(mode='overview',summary=''),dict(mode='timeline',chapters=self.timeline()['chapters'][:1]),
                        dict(mode='guess',summary='内容'),dict(mode='overview',summary='内容',typo='bad')])
        for payload in invalid:
            with self.subTest(payload=payload),self.assertRaises(ValueError):summaries.save(self.job,payload)
            self.assertEqual((self.job/'summary.json').read_bytes(),before)

    def test_stale_summary_and_unreviewed_input_are_rejected(self):
        summaries.save(self.job,dict(mode='overview',summary='围绕目标展开。'))
        source=d.read_json(self.job/'source.json');source['title']='来源已更新';d.write_json(self.job/'source.json',source)
        with self.assertRaisesRegex(ValueError,'总结已过期'):checked_export(self.job,self.job/'out')
        state=d.read_json(self.job/'review-state.json');state['reviewed_ids']=['a'];d.write_json(self.job/'review-state.json',state)
        with self.assertRaisesRegex(ValueError,'未校对'):summaries.save(self.job,dict(mode='overview',summary='新总结。'))

    def test_summary_required_flag_and_updated_summary_change_export(self):
        report=quality.check(self.job);quality.accept(self.job,dict(snapshot_fingerprint=report['snapshot_fingerprint'],accepted_issues=[]))
        with self.assertRaisesRegex(ValueError,'缺少总结'):d.finalize(self.job,self.job/'out',require_summary=True)
        summaries.save(self.job,dict(mode='overview',summary='明确目标。'))
        first=d.finalize(self.job,self.job/'out',require_summary=True)
        summaries.save(self.job,dict(mode='overview',summary='明确目标，并给出方案与安排。'))
        second=d.finalize(self.job,self.job/'out',require_summary=True)
        self.assertNotEqual(first['path'],second['path'])
        self.assertIn('并给出方案',Path(second['path']).read_text(encoding='utf-8'))

    def test_changed_transcript_review_or_audio_invalidates_saved_summary(self):
        for name,change in [('turns.corrected.json',lambda x:[{**x[0],'text':'目标已经改变。'},*x[1:]]),
                            ('review-state.json',lambda x:{**x,'reviewer':'changed'})]:
            with self.subTest(file=name):
                summaries.save(self.job,dict(mode='overview',summary='明确目标。'))
                path=self.job/name;before=path.read_bytes()
                d.write_json(path,change(d.read_json(path)))
                with self.assertRaisesRegex(ValueError,'总结已过期'):summaries.load(self.job)
                path.write_bytes(before)
        summaries.save(self.job,dict(mode='overview',summary='明确目标。'))
        (self.job/'audio.wav').write_bytes(b'changed audio')
        with self.assertRaisesRegex(ValueError,'总结已过期'):summaries.load(self.job)

    def test_pending_export_cannot_replay_old_summary(self):
        summaries.save(self.job,dict(mode='overview',summary='明确目标。'))
        report=quality.check(self.job);quality.accept(self.job,dict(snapshot_fingerprint=report['snapshot_fingerprint'],accepted_issues=[]))
        old=summaries.load(self.job)
        d._write_transaction(self.job/'export-transaction.json',dict(policy_version=2,quality_fingerprint=quality.require_accepted(self.job)['fingerprint'],
                     summary_fingerprint=d._digest(old),files=[],manifest_update={}))
        summaries.save(self.job,dict(mode='overview',summary='新的总结。'))
        with self.assertRaisesRegex(ValueError,'总结已变化'):d._recover_export(self.job)


if __name__=='__main__':unittest.main()
