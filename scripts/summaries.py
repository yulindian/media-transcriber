"""Agent-written summaries bound to the reviewed transcript; no model API."""
from pathlib import Path
import re
import documents as docs


def _context(job,validate=False):
    job=Path(job);docs.recover_review(job)
    turns=docs.read_json(job/'turns.corrected.json');state=docs.read_json(job/'review-state.json')
    manifest=docs.read_json(job/'manifest.json')
    # Validate once when saving. Loads bind the exact reviewed data by hash;
    # replaying the entire edit history again on each export adds needless work.
    if validate:docs._validate_audit(docs.read_json(job/'turns.raw.json'),turns,state)
    docs.validate_final(manifest,turns,state['reviewed_ids'])
    fingerprint=docs._digest({'version':1,'turns':turns,'review':state,
        'source':docs.read_json(job/'source.json'),
        'audio':docs.file_hash(job/'audio.wav') if (job/'audio.wav').exists() else None,
        'coverage':{k:manifest.get(k) for k in ('audio_duration','coverage_complete','coverage_basis','limit_seconds')}})
    return turns,fingerprint


def _text(value,limit):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValueError('总结文字不能为空或超出长度限制')
    return re.sub(r'\s+',' ',value).strip()


def _validate(payload,turns):
    if not isinstance(payload,dict):raise ValueError('总结必须是 JSON 对象')
    mode=payload.get('mode')
    if mode=='overview':
        if set(payload)!={'mode','summary'}:raise ValueError('单主题总结仅使用 mode、summary')
        return dict(mode=mode,summary=_text(payload['summary'],6000))
    if mode!='timeline' or set(payload)!={'mode','chapters'}:
        raise ValueError('总结模式必须为 overview 或 timeline')
    chapters=payload['chapters']
    if not isinstance(chapters,list) or not 2<=len(chapters)<=50:
        raise ValueError('多主题时间轴需要2至50个话题；单一主题使用 overview')
    indexes={t['id']:i for i,t in enumerate(turns)};expected=0;result=[]
    for chapter in chapters:
        if not isinstance(chapter,dict) or set(chapter)!={'start_turn_id','end_turn_id','title','summary'}:
            raise ValueError('时间轴条目需要起止段落ID、标题和总结')
        if not all(isinstance(chapter[key],str) for key in ('start_turn_id','end_turn_id')):
            raise ValueError('时间轴段落ID必须是字符串')
        start=indexes.get(chapter['start_turn_id']);end=indexes.get(chapter['end_turn_id'])
        if start is None or end is None or start!=expected or end<start:
            raise ValueError('时间轴段落必须存在、按顺序连续覆盖且不能重叠')
        result.append(dict(start_turn_id=chapter['start_turn_id'],end_turn_id=chapter['end_turn_id'],
                           title=_text(chapter['title'],100),summary=_text(chapter['summary'],3000)))
        expected=end+1
    if expected!=len(turns):raise ValueError('时间轴总结未覆盖全部正文段落')
    return dict(mode=mode,chapters=result)


def save(job,payload):
    turns,fingerprint=_context(job,validate=True);content=_validate(payload,turns)
    docs.write_json(Path(job)/'summary.json',dict(version=1,source_fingerprint=fingerprint,content=content))
    return dict(status='summary_saved',mode=content['mode'],chapter_count=len(content.get('chapters',[])),next='finalize --require-summary')


def load(job,required=False):
    path=Path(job)/'summary.json'
    if not path.exists():
        if required:raise ValueError('缺少总结，请基于校对全文执行 apply-summary')
        return None
    turns,fingerprint=_context(job);record=docs.read_json(path)
    if not isinstance(record,dict) or record.get('version')!=1 or record.get('source_fingerprint')!=fingerprint:
        raise ValueError('总结已过期，请基于当前校对全文重新执行 apply-summary')
    return {**record,'content':_validate(record['content'],turns)}


def render(record,turns):
    content=record['content']
    if content['mode']=='overview':return '## 内容总结\n\n'+content['summary']+'\n\n'
    indexes={t['id']:i for i,t in enumerate(turns)};parts=['## 时间轴总结']
    for chapter in content['chapters']:
        selected=turns[indexes[chapter['start_turn_id']]:indexes[chapter['end_turn_id']]+1]
        start=min(t['start'] for t in selected);end=max(t['end'] for t in selected)
        parts.append('### '+docs.clock(start)+'–'+docs.clock(end)+'｜'+chapter['title'])
        parts.append(chapter['summary'])
    return '\n\n'.join(parts)+'\n\n'
