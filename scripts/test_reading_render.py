from test_support import checked_export
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import documents as d


def turn(tid, text, start, end, speaker='说话人1'):
    return {'id':tid, 'text':text, 'start':start, 'end':end, 'speaker':speaker}


class ReadingRenderTests(unittest.TestCase):
    def setUp(self):
        self.info = {'id':'one', 'title':'演示', 'platform':'视频号',
                     'source_url':'https://weixin.qq.com/sph/example'}
        self.manifest = {'created_at':'2026-11-03', 'coverage_complete':True,
                         'audio_duration':50.0, 'speaker_report':{'count':1, 'count_mode':'estimated'}}

    def render(self, turns, corrected=True):
        return d.markdown(self.info, self.manifest, turns, corrected)

    def frontmatter(self, text):
        return {key:json.loads(value) for key,value in
                (line.split(': ',1) for line in text.split('---\n',2)[1].splitlines())}

    def test_split_chinese_word_and_number_are_joined_for_reading(self):
        turns = [turn('t1','前',0,25),turn('t2','端写了百分之九',24.95,50),turn('t3','十九。',50.1,51)]
        original = copy.deepcopy(turns)
        text = self.render(turns)
        self.assertIn('前端写了百分之九十九。',text)
        self.assertNotIn('**说话人1**',text)
        self.assertEqual(turns,original)

    def test_speaker_uncertainty_gap_and_sentence_end_keep_boundaries(self):
        cases = [
            (turn('t1','话没说完',0,1),turn('t2','继续。',1.1,2,'说话人2')),
            (turn('t1','话没说完',0,1,'说话人1（待核对）'),turn('t2','继续。',1.1,2,'说话人1（待核对）')),
            (turn('t1','话没说完',0,1,'说话人未确定'),turn('t2','继续。',1.1,2,'说话人未确定')),
            (turn('t1','话没说完',0,1,'说话人1/说话人2'),turn('t2','继续。',1.1,2,'说话人1/说话人2')),
            (turn('t1','话没说完',0,1),turn('t2','继续。',2.01,3)),
            (turn('t1','这是完整句子。”',0,1),turn('t2','下一句。',1.1,2)),
        ]
        for a,b in cases:
            with self.subTest(a=a,b=b):
                text = self.render([a,b])
                self.assertNotIn(f"[{d.clock(b['start'])}]",text)
                if a['speaker']!=b['speaker'] or any(x in b['speaker'] for x in ('待核对','未确定','/')):
                    self.assertIn(f"**{b['speaker'].replace('（待核对）','')}**",text)

    def test_single_speaker_reading_uses_paragraphs_without_role_labels(self):
        text=self.render([turn('t1','先准备材料。',0,3),turn('t2','接着开始操作。',4,8)])
        content=text.split('## 正文\n\n',1)[1]
        self.assertEqual(content,'先准备材料。\n\n接着开始操作。\n')

    def test_multiple_or_uncertain_speakers_keep_labels_even_with_count_one(self):
        for speakers in [('说话人1','说话人2'),('说话人1','说话人未确定'),
                         ('说话人1（待核对）','说话人1（待核对）'),('说话人1/说话人2','说话人1/说话人2')]:
            with self.subTest(speakers=speakers):
                text=self.render([turn('t1','你好。',0,2,speakers[0]),turn('t2','你好。',3,5,speakers[1])])
                self.assertIn(f"**{speakers[0].replace('（待核对）','')}**",text)
                self.assertIn(f"**{speakers[1].replace('（待核对）','')}**",text)

    def test_reading_hides_review_annotations_but_preserves_speech_and_audit(self):
        turns=[turn('t1','术语[词句待核对]，接续（片段接续待核对）。\n\n这件事有待核对，保留[示例]和（二选一）。',0,4,'说话人1（待核对）'),
               turn('t2','名字【称呼待核对】(片段待核对)，继续。',5,8,'说话人2')]
        original=copy.deepcopy(turns)
        content=self.render(turns).split('## 正文\n\n',1)[1]
        self.assertEqual(content,'**说话人1**：术语，接续。\n\n这件事有待核对，保留[示例]和（二选一）。\n\n**说话人2**：名字，继续。\n')
        raw=self.render(turns,False)
        self.assertIn('[00:00:00]',raw)
        self.assertIn('[词句待核对]',raw)
        self.assertEqual(turns,original)

    def test_single_visible_voice_does_not_override_multiple_or_unknown_count(self):
        for report in ({'count':2},{}):
            with self.subTest(report=report):
                self.manifest['speaker_report']=report
                self.assertIn('**说话人1**',self.render([turn('t1','你好。',0,2)]))

    def test_join_keeps_existing_natural_paragraphs(self):
        text = self.render([turn('t1','第一段。\n\n第二段的前',0,25),turn('t2','端。',25.2,27)])
        self.assertIn('第一段。\n\n第二段的前端。',text)

    def test_ambiguous_fragments_between_same_voice_are_joined_for_reading_only(self):
        turns=[turn('a','分享一下。',0,3,'说话人2'),
               turn('b','我',3,3.2,'说话人1/说话人2'),
               turn('c','就说，准备好了',3.2,6,'说话人2'),
               turn('d','所以他',6,6.3,'说话人1/说话人2'),
               turn('e','说，那我来。',6.3,9,'说话人2')]
        original=copy.deepcopy(turns)
        text=self.render(turns).split('## 正文\n\n',1)[1]
        self.assertIn('我就说，准备好了所以他说，那我来。',text)
        self.assertNotIn('**说话人1/说话人2**',text)
        self.assertEqual(turns,original)

    def test_long_continuation_never_splits_inside_a_sentence_or_number(self):
        first='这里先介绍流程。'*60+'填好就能用，然后'
        text=self.render([turn('a',first,0,25),turn('b','继续创建项目。批量复制，每次替换一',25,50),
                          turn('c','个部分。下一步处理。',49.86,60)]).split('## 正文\n\n',1)[1]
        self.assertIn('然后继续创建项目。',text)
        self.assertIn('替换一个部分。',text)

    def test_fragment_bridge_keeps_real_switches_gaps_and_unknown_voices(self):
        for middle,right,start in [('说话人1/说话人2','说话人2',2),
                                   ('说话人1/说话人2','说话人1',5),
                                   ('说话人未确定','说话人1',2)]:
            with self.subTest(middle=middle,right=right,start=start):
                turns=[turn('a','说到这里。',0,1,'说话人1'),turn('b','我',1,1.2,middle),
                       turn('c','继续说。',start,start+1,right)]
                groups=d._reading_turns(turns)
                self.assertEqual(groups[1]['speaker'],middle)
                self.assertEqual(groups[1]['text'],'我')

    def test_reading_omits_only_filler_responses_and_preserves_meaningful_answers(self):
        turns=[turn('a','嗯',0,0.4),turn('b','不',1,1.4),turn('c','是',2,2.4),turn('d','对',3,3.4)]
        text=self.render(turns).split('## 正文\n\n',1)[1]
        self.assertNotIn('嗯',text)
        self.assertIn('不是对',text)
        self.assertIn('嗯',self.render(turns,False))

    def test_internal_accidental_breaks_are_repaired_but_sentences_stay_separate(self):
        text=self.render([turn('a','完整句子。\n\n然后\n\n继续执行，一\n\n个部分。',0,4)])
        self.assertIn('完整句子。\n\n然后继续执行，一个部分。',text)

    def test_raw_markdown_retains_original_time_slices(self):
        turns = [turn('t1','前',0,25),turn('t2','端。',25,26)]
        text = self.render(turns,False)
        self.assertEqual(text.count('**说话人1**'),2)
        self.assertIn('[00:00:25]',text)

    def test_body_hash_matches_actual_rendered_reading_body(self):
        text = self.render([turn('t1','前',0,25),turn('t2','端。',25,26)])
        attrs = self.frontmatter(text)
        content = text.split('## 正文\n\n',1)[1]
        self.assertEqual(attrs['body_sha256'],hashlib.sha256(content.encode()).hexdigest())

    def test_source_and_duration_provenance_exported_without_credentials(self):
        self.info.update(acquisition='anonymous_api',duration=50.5,
                         media_url='https://media.example/video?token=SECRET',cookie='SECRET',
                         acquisition_attempts=[{'route':'yt-dlp','status':'failed','message':'https://api.example/?token=SECRET Cookie: SECRET'},
                                               {'route':'anonymous_api','status':'success','endpoint':'https://api.example/parse?key=SECRET'}])
        text = self.render([turn('t1','测试。',0,1)])
        attrs = self.frontmatter(text)
        self.assertEqual(attrs['acquisition'],'anonymous_api')
        self.assertEqual(attrs['duration_expected'],50.5)
        self.assertEqual(attrs['coverage_basis'],'source_duration_checked')
        self.assertEqual(len(attrs['acquisition_attempts']),2)
        self.assertNotIn('SECRET',text)
        self.assertNotIn('media.example',text)

    def test_missing_source_duration_discloses_acquired_audio_scope(self):
        attrs = self.frontmatter(self.render([turn('t1','测试。',0,1)]))
        self.assertIsNone(attrs['duration_expected'])
        self.assertEqual(attrs['coverage_basis'],'acquired_audio_only')

    def test_finalization_keeps_srt_and_audit_time_slices(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            turns = [turn('t1','前',0,25),turn('t2','端。',25.1,26)]
            state = {'reviewed_ids':['t1','t2'],'edits':[]}
            for name,data in [('source.json',self.info),('manifest.json',self.manifest),
                              ('turns.raw.json',turns),('turns.corrected.json',turns),('review-state.json',state)]:
                d.write_json(job/name,data)
            before = {name:(job/name).read_bytes() for name in ('turns.raw.json','turns.corrected.json','review-state.json')}
            result = checked_export(job,job/'out')
            text = Path(result['path']).read_text(encoding='utf-8')
            self.assertIn('前端。',text)
            srt = Path(result['subtitle']).read_text(encoding='utf-8')
            self.assertIn('00:00:00,000 --> 00:00:25,000\n说话人1：前',srt)
            self.assertIn('00:00:25,100 --> 00:00:26,000\n说话人1：端。',srt)
            self.assertEqual(before,{name:(job/name).read_bytes() for name in before})
            self.assertEqual(checked_export(job,job/'out')['status'],'existing')


if __name__ == '__main__':unittest.main()
