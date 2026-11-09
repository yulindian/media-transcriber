"""Auditable edits and UTF-8 Markdown output; no external language-model API."""
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import privacy


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, data):
    _write_text(path,json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def _write_text(path, text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:
            f.write(text);f.flush();os.fsync(f.fileno())
        Path(name).replace(path)
        if path.read_text(encoding='utf-8')!=text:raise ValueError('Write verification failed')
    finally:
        Path(name).unlink(missing_ok=True)


def _lexical_size(value):
    return sum(unicodedata.category(c)[0] in 'LN' for c in value)


def _digest(data):
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')).hexdigest()


def _read_transaction(path):
    data=read_json(path)
    if not isinstance(data,dict) or 'payload' not in data or data.get('sha256')!=_digest(data['payload']):
        raise ValueError('Damaged transaction journal; recovery stopped')
    return data['payload']


def _write_transaction(path,payload):
    write_json(path,{'payload':payload,'sha256':_digest(payload)})


def _validate_turns(turns):
    if not isinstance(turns,list) or not turns:raise ValueError('Empty or malformed transcript')
    ids=set();previous=-1
    for t in turns:
        if not isinstance(t,dict):raise ValueError('Malformed turn')
        tid=t.get('id')
        if not isinstance(tid,str) or not tid.strip() or tid in ids:raise ValueError('Invalid or duplicate turn ID')
        ids.add(tid)
        for key in ('start','end'):
            v=t.get(key)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):raise ValueError('Invalid timestamp')
        if t['start']<previous or t['start']<0 or t['end']<t['start']:raise ValueError('Invalid turn timing')
        previous=t['start']
        if not isinstance(t.get('speaker'),str) or not t['speaker'].strip():raise ValueError('Invalid speaker')
        if not isinstance(t.get('text'),str) or not _lexical_size(t['text']):raise ValueError('Transcript text must contain words')


def _validate_audit(raw, corrected, state):
    _validate_turns(raw);_validate_turns(corrected)
    if len(raw)!=len(corrected) or any({k:v for k,v in a.items() if k!='text'}!={k:v for k,v in b.items() if k!='text'} for a,b in zip(raw,corrected)):
        raise ValueError('Transcript structure changed')
    reviewed=state.get('reviewed_ids')
    if not isinstance(reviewed,list) or any(not isinstance(i,str) for i in reviewed) or len(set(reviewed))!=len(reviewed) or not set(reviewed)<=set(t['id'] for t in raw):raise ValueError('Invalid reviewed_ids')
    edits=state.get('edits')
    if not isinstance(edits,list):raise ValueError('Invalid audit log')
    replay=copy.deepcopy(raw)
    by_id={t['id']:t for t in replay}
    for edit in edits:
        if not isinstance(edit,dict):raise ValueError('Invalid audit entry')
        tid=edit.get('turn_id')
        if not isinstance(tid,str) or tid not in by_id or tid not in reviewed:raise ValueError('Invalid audit turn')
        if edit.get('before')!=by_id[tid]['text']:raise ValueError('Broken audit before/after chain')
        if not all(isinstance(edit.get(k),str) and edit[k].strip() for k in ('after','reason','evidence')):raise ValueError('Incomplete audit entry')
        # Historical edits may predate conservative review guards and be corrected
        # by later entries. Replay records faithfully; validate the final text below.
        by_id[tid]['text']=edit['after']
    if replay!=corrected:raise ValueError('Corrected transcript does not match audit replay')


def recover_review(job):
    """Roll forward an interrupted save before reading review state."""
    job=Path(job);journal=job/'review-transaction.json';reset=job/'review-reset-transaction.json'
    if reset.exists():
        if journal.exists():raise ValueError('Conflicting review transactions; recovery stopped to preserve history')
        data=_read_transaction(reset)
        _validate_audit(data['turns'],data['turns'],data['state'])
        # Preserve the exact old bytes, including malformed JSON, before replacing
        # any part of the old review. A failed backup leaves the reset pending.
        write_json(job/data['backup_name'],data['backup'])
        write_json(job/'turns.raw.json',data['turns'])
        write_json(job/'turns.corrected.json',data['turns'])
        write_json(job/'review-state.json',data['state'])
        reset.unlink()
    if journal.exists():
        data=_read_transaction(journal)
        _validate_audit(read_json(job/'turns.raw.json'),data['turns'],data['state'])
        write_json(job/'turns.corrected.json',data['turns'])
        write_json(job/'review-state.json',data['state'])
        journal.unlink()


def reset_review(job, turns):
    """Initialize a new raw/corrected/review triple with recoverable history.

    Recover outstanding edits first; invalid pending edits fail without resetting.
    On write failure, recover_review completes the reset on the next locked call.
    """
    job=Path(job);recover_review(job);_validate_turns(turns)
    state={'reviewed_ids':[],'edits':[],'reviewer':'Invoking agent conservative ASR correction'}
    backup={}
    for name in ('turns.raw.json','turns.corrected.json','review-state.json'):
        path=job/name
        backup[name]=path.read_bytes().hex() if path.exists() else None
    payload={'turns':turns,'state':state,'backup':{'encoding':'hex','files':backup},
             'backup_name':'review-reset-backup-'+uuid.uuid4().hex+'.json'}
    _write_transaction(job/'review-reset-transaction.json',payload)
    recover_review(job)


def save_review(job, new, state):
    """Durably journal a validated pair, then save it; retry/recover on failure.

    Call recover_review(job) BEFORE reading the pair to construct a new edit.
    Single writer only. reviewed_ids records a caller assertion, not proof of reading.
    """
    job=Path(job);recover_review(job)
    _validate_audit(read_json(job/'turns.raw.json'),new,state)
    _write_transaction(job/'review-transaction.json',{'turns':new,'state':state})
    recover_review(job)


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def clock(t):
    n=int(t);return f'{n//3600:02d}:{n//60%60:02d}:{n%60:02d}'


def filename(text):
    text=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',str(text)).strip(' .')[:90] or '音视频转录'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',text,re.I):text='_'+text
    return text


_ENTITY = re.compile(r'(?:百分之)?(?:[+-]?\d+(?:[.,:/-]\d+)*|[零〇一二两三四五六七八九十百千万亿点]+)\s*(?:人民币|美元|美金|万元|亿元|摄氏度|华氏度|毫秒|分钟|小时|公里|千克|公斤|厘米|毫米|毫升|元|块|秒|分|时|天|日|周|月|年|岁|个|人|件|倍|米|克|升|度|℃|℉|°[CF]|%|％|(?:kg|mg|km|cm|mm|ml|usd|cny|rmb|gb|mb|kb|khz|mhz|hz|ms|seconds|minutes|hours|days|years|g|m|s|h|l)(?![A-Za-z]))?',re.I)


def entities(text):
    return [re.sub(r'\s+','',m.group()) for m in _ENTITY.finditer(unicodedata.normalize('NFKC',text))]


def _entity_evidence(edit, turn, job):
    evidence=edit.get('entity_review')
    if job is None or not isinstance(evidence,dict) or evidence.get('method')!='audio_excerpt_review':
        raise ValueError('关键数字/日期/比例/单位变化，需要 entity_review 和对应音轨片段复核')
    if not isinstance(evidence.get('note'),str) or not evidence['note'].strip():raise ValueError('Missing entity review note')
    if not isinstance(evidence.get('quote'),str) or entities(evidence['quote'])!=entities(edit['after']):
        raise ValueError('复核 quote 中的关键实体必须与修改后文本一致')
    root=(Path(job)/'review-clips').resolve()
    clip=Path(evidence.get('clip_path','')).resolve()
    if not clip.is_relative_to(root) or clip.suffix!='.wav' or not clip.is_file():raise ValueError('Entity evidence clip must be inside this job/review-clips')
    record=read_json(clip.with_suffix('.json'))
    audio=Path(job)/'audio.wav'
    if (record.get('turn_sha256')!=_digest(turn) or record.get('turn_id')!=turn['id']
            or record.get('clip_sha256')!=file_hash(clip) or not audio.is_file()
            or record.get('audio_sha256')!=file_hash(audio)):
        raise ValueError('Entity audio evidence is stale or changed; extract and review again')


def apply_review(turns, review, job=None):
    _validate_turns(turns)
    if not isinstance(review,dict):raise ValueError('Invalid review')
    updated=copy.deepcopy(turns);by_id={t['id']:t for t in updated}
    reviewed=review.get('reviewed_ids',[])
    if not isinstance(reviewed,list) or any(not isinstance(i,str) for i in reviewed):raise ValueError('Invalid reviewed_ids')
    if len(set(reviewed))!=len(reviewed) or not set(reviewed)<=set(by_id):
        raise ValueError('reviewed_ids 包含重复或不存在的段落')
    edits=review.get('edits',[]);seen=set()
    if not isinstance(edits,list):raise ValueError('Invalid edits')
    for e in edits:
        if not isinstance(e,dict):raise ValueError('Invalid edit')
        tid=e.get('turn_id')
        if tid not in by_id or tid in seen or tid not in reviewed:raise ValueError('修改段落必须唯一且标记已校对')
        seen.add(tid);t=by_id[tid]
        if t['text']!=e.get('before'):raise ValueError('before 与当前原文不一致，拒绝过期修改')
        after=e.get('after')
        if not isinstance(after,str) or not after.strip() or not e.get('reason') or not e.get('evidence'):
            raise ValueError('纠错需要完整 after、reason、evidence，不允许删除整段')
        if not _lexical_size(after):raise ValueError('Replacement must contain words')
        if not all(isinstance(e.get(k),str) and e[k].strip() for k in ('reason','evidence')):raise ValueError('Missing review explanation')
        if entities(t['text'])!=entities(after):_entity_evidence(e,t,job)
        # Conservative lexical guard, not a general semantic equivalence test.
        for token in ('不','没','无','未','别','非','勿','莫','not','never','no',"n't"):
            count=lambda s: len(re.findall(r'\b'+token+r'\b',s,re.I)) if token.isascii() and token!="n't" else s.lower().count(token)
            if count(t['text'])!=count(after):raise ValueError('Negation changed; requires separate explicit semantic review')
        ratio=_lexical_size(after)/max(_lexical_size(t['text']),1)
        if not .6<=ratio<=1.6:raise ValueError('改动长度异常，可能在摘要或扩写；请拆成保留原意的纠错')
        t['text']=after
    return updated, edits, reviewed


def preflight_review(turns, review, job=None):
    """Collect per-edit errors without changing turns or review progress."""
    errors=[]
    if not isinstance(review,dict) or not isinstance(review.get('edits',[]),list):
        return {'valid':False,'errors':[{'message':'Invalid review or edits'}]}
    ids=review.get('reviewed_ids',[])
    try:apply_review(turns,{'reviewed_ids':ids,'edits':[]})
    except ValueError as exc:
        return {'valid':False,'errors':[{'message':str(exc)}]}
    seen=set()
    by_id={t['id']:t for t in turns}
    for index,e in enumerate(review.get('edits',[])):
        tid=e.get('turn_id') if isinstance(e,dict) else None
        try:
            if not isinstance(tid,str) or tid not in by_id or tid not in ids:
                raise ValueError('修改段落必须存在且标记已校对')
            if isinstance(tid,str) and tid in seen:raise ValueError('Duplicate edit ID')
            apply_review([by_id[tid]],{'reviewed_ids':[tid],'edits':[e]},job)
        except (ValueError,OSError) as exc:errors.append({'index':index,'turn_id':tid,'message':str(exc)})
        if isinstance(tid,str):seen.add(tid)
    return {'valid':not errors,'errors':errors}


def validate_final(manifest, turns, reviewed):
    if manifest.get('limit_seconds') is not None or not manifest.get('coverage_complete'):raise ValueError('音频仅处理部分或尚未完成，不能发布完整稿')
    if manifest.get('status') not in (None,'review_pending','complete'):
        raise ValueError('当前任务状态不能导出完整稿')
    if set(t['id'] for t in turns)!=set(reviewed):raise ValueError('仍有段落未校对，不能将草稿称为已纠错全文')


def _reading_turns(turns):
    """Join continuations for display only; timestamps and audit turns stay intact."""
    grouped=[];previous=None
    sentence_end=re.compile(r'''[。！？!?…\.][”’"')）】》」』]*$''')
    for t in turns:
        certain=not any(marker in t['speaker'] for marker in ('待核对','未确定','/'))
        continuation=(previous is not None and certain
                      and previous['speaker']==t['speaker']
                      and -.1<=t['start']-previous['end']<=1
                      and len(grouped[-1]['text'])+len(t['text'])<=320
                      and not sentence_end.search(previous['text'].rstrip()))
        if continuation:
            # Do not add whitespace: a slice may split one word or number.
            # Existing punctuation, spaces and natural paragraphs remain verbatim.
            grouped[-1]['text']+=t['text']
            grouped[-1]['end']=max(grouped[-1]['end'],t['end'])
        else:
            grouped.append(dict(t))
        previous=t
    return grouped


def _without_review_notes(text):
    """Hide bracketed editorial notes in reading exports, preserving source data."""
    return re.sub(r'\[[^\[\]\n]{0,32}待核对\]|【[^【】\n]{0,32}待核对】|（[^（）\n]{0,32}待核对）|\([^()\n]{0,32}待核对\)', '', text)


def body(turns, reading=False, show_speakers=True):
    displayed=_reading_turns(turns) if reading else turns
    def paragraphs(text):
        # Split only at existing punctuation; never invent intra-turn timestamps.
        chunks=[];current=''
        for sentence in re.split(r'(?<=[。！？!?；;])',text):
            if current and len(current)+len(sentence)>160:
                chunks.append(current);current=''
            current+=sentence
        if current:chunks.append(current)
        return '\n\n'.join(chunks)
    chunks=[]
    for t in displayed:
        text=_without_review_notes(t['text']) if reading else t['text']
        if not text.strip():continue
        speaker=_without_review_notes(t['speaker']) if reading else t['speaker']
        prefix='' if reading else f"[{clock(t['start'])}] "
        chunks.append(prefix+(f'**{speaker}**：' if show_speakers else '')+(paragraphs(text) if reading else text))
    return '\n\n'.join(chunks)+'\n'


def _duration_expected(info, manifest):
    value=manifest.get('duration_expected',info.get('duration'))
    if isinstance(value,bool):return None
    try:value=float(value)
    except (TypeError,ValueError):return None
    return value if math.isfinite(value) and value>0 else None


def _acquisition_attempts(info):
    """Publish route provenance, not upstream errors, headers or signed media URLs."""
    records=info.get('acquisition_attempts',[])
    if not isinstance(records,list):return []
    safe=[]
    for record in records:
        if not isinstance(record,dict):continue
        entry={key:record[key] for key in ('route','status') if isinstance(record.get(key),str)}
        endpoint=record.get('endpoint')
        if isinstance(endpoint,str):
            try:
                parsed=urlsplit(endpoint)
                if parsed.scheme in ('http','https') and parsed.hostname:
                    # Omit userinfo, query and fragment, which can hold credentials.
                    host=parsed.hostname
                    if ':' in host:host='['+host+']'
                    if parsed.port:host+=':'+str(parsed.port)
                    entry['endpoint']=urlunsplit((parsed.scheme,host,parsed.path,'',''))
            except ValueError:pass
        if entry:safe.append(entry)
    return safe


def markdown(info, manifest, turns, corrected=False):
    count=manifest.get('speaker_report',{}).get('count')
    labels={t['speaker'] for t in turns}
    single=(corrected and type(count) is int and count==1 and len(labels)==1
            and re.fullmatch(r'说话人\d+',next(iter(labels))) is not None)
    content=body(turns,reading=corrected,show_speakers=not single)
    attrs={'type':'media-transcript','title':info['title'],'source_platform':info.get('platform',''),'source_url':privacy.source_url(info['source_url']),'source_id':info['id'],'captured_at':manifest['created_at'],'audio_duration_seconds':manifest.get('audio_duration'),'body_complete':manifest.get('coverage_complete',False),'transcript_status':'已纠错机器转录' if corrected else '机器原始转录，未校对','speaker_count_mode':manifest.get('speaker_report',{}).get('count_mode','unavailable'),'speaker_count':manifest.get('speaker_report',{}).get('count'),'speaker_accuracy_verified':False,'audio_manually_verified':False,'body_sha256':hashlib.sha256(content.encode()).hexdigest()}
    expected=_duration_expected(info,manifest)
    coverage_basis=manifest.get('coverage_basis')
    if coverage_basis not in ('source_duration_checked','acquired_audio_only','partial_audio'):
        coverage_basis='source_duration_checked' if expected is not None else 'acquired_audio_only'
    attrs.update(acquisition=info.get('acquisition','unspecified'),acquisition_attempts=_acquisition_attempts(info),asr_language=manifest.get('asr_language','zh'),uses_user_credentials=info.get('uses_user_credentials'),
                 duration_expected=expected,coverage_basis=coverage_basis)
    attrs.update(text_review_complete=bool(corrected),audio_review_status='not_verified',
                 speaker_review_status='not_verified',render_version=5)
    if corrected:attrs.update(manifest.get('quality_delivery',{}))
    yaml='---\n'+'\n'.join(k+': '+json.dumps(v,ensure_ascii=False) for k,v in attrs.items())+'\n---\n'
    warning='说话人编号为声纹估计，不代表真实姓名。短应答、抢话、笑声可能串人；“待核对”不能涵盖所有错误。'
    if corrected:warning='说话人标签为声音特征估计，未经逐句听审；复核信息见纠错记录。'
    if single:warning='本稿按单人发言排版，正文省略说话人标签；人数为识别估计，未经逐句听审。'
    if manifest.get('speaker_report',{}).get('limited_evidence'):warning+=' 有效语音证据不足，短发言归属需核对。'
    if coverage_basis=='acquired_audio_only':warning+=' 完整性仅覆盖已取得的音轨，未核验源平台总时长。'
    if not manifest.get('coverage_complete'):warning='【部分音频试跑，非完整稿】'+warning
    if corrected and manifest.get('quality_delivery',{}).get('accepted_issue_turns'):
        warning+=' 本稿保留 '+str(manifest['quality_delivery']['accepted_issue_turns'])+' 段已说明原因的疑点，详见纠错记录中的 quality_acceptance。'
    return yaml+'\n# '+info['title'].replace('\n',' ')+'\n\n'+warning+'\n\n## 正文\n\n'+content


def draft(job):
    job=Path(job);m=read_json(job/'manifest.json');info=read_json(job/'source.json');turns=read_json(job/'turns.raw.json')
    text=markdown(info,m,turns)
    (job/'原始识别稿.md').write_text(text,encoding='utf-8')
    assert (job/'原始识别稿.md').read_text(encoding='utf-8')==text


_EXPORT_FIELDS = {'status','final_path','final_sha256','review_sha256','final_turns_sha256',
                  'export_fingerprint','export_artifacts'}


def _recover_export(job):
    journal=job/'export-transaction.json'
    if not journal.exists():return
    data=_read_transaction(journal)
    if data.get('policy_version')!=2:
        raise ValueError('旧版导出事务不满足当前隐私/验收策略；请保留并移出任务目录后重新 quality、accept-quality、finalize')
    import quality
    acceptance=quality.require_accepted(job)
    if data.get('quality_fingerprint')!=acceptance['fingerprint']:
        raise ValueError('待恢复导出对应的质量验收已过期；保留现场，不能发布旧稿')
    for record in data['files']:
        _write_text(record['path'],record['text'])
    m=read_json(job/'manifest.json');m.update(data['manifest_update'])
    write_json(job/'manifest.json',m)
    journal.unlink()


def finalize(job, output):
    job=Path(job);recover_review(job);_recover_export(job)
    m=read_json(job/'manifest.json');info=read_json(job/'source.json');raw=read_json(job/'turns.raw.json')
    corrected=read_json(job/'turns.corrected.json');review=read_json(job/'review-state.json')
    _validate_audit(raw,corrected,review)
    validate_final(m,corrected,review['reviewed_ids'])
    review_input=m.get('review_input_sha256')
    if review_input is not None:
        audio=job/'audio.wav'
        if not audio.is_file() or _digest({'audio':file_hash(audio),'turns':raw})!=review_input:
            raise ValueError('音频或原稿已变化，请重新运行任务再导出')
    import quality
    acceptance=quality.require_accepted(job)
    m['quality_delivery']=acceptance['delivery']
    out=Path(output).resolve();out.mkdir(parents=True,exist_ok=True)
    def stamp(t):
        ms=round(t*1000);return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}'
    texts={'path':markdown(info,m,corrected,True),'raw_path':markdown(info,m,raw),
           'correction_log':json.dumps(privacy.audit({**review,'quality_acceptance':acceptance}),ensure_ascii=False,indent=2)+'\n',
           'subtitle':'\n\n'.join(f"{i}\n{stamp(t['start'])} --> {stamp(t['end'])}\n{t['speaker']}："+'\n'.join(line.strip() for line in t['text'].splitlines() if line.strip()) for i,t in enumerate(corrected,1))+'\n'}
    rendered_manifest={k:m[k] for k in ('created_at','audio_duration','coverage_complete','speaker_report','duration_expected','coverage_basis') if k in m}
    inputs={'manifest':rendered_manifest,'source':info,
            'raw':raw,'corrected':corrected,'review':review,'quality_acceptance':acceptance}
    fingerprint=hashlib.sha256(json.dumps(inputs,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    records=m.get('export_artifacts',{})
    if m.get('export_fingerprint')==fingerprint and set(records)==set(texts):
        valid=True
        for key,text in texts.items():
            record=records[key];path=Path(record['path'])
            expected=hashlib.sha256(text.encode('utf-8')).hexdigest()
            if path.parent.resolve()!=out or not path.is_file() or record.get('sha256')!=expected or file_hash(path)!=expected:
                valid=False;break
        if valid:return dict(status='existing',verified=True,verification_scope='artifact_integrity',**acceptance['delivery'],**{k:v['path'] for k,v in records.items()})
    base=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')+'-'+filename(info['title'])+'-'+filename(info['id'])[:20]
    suffixes={'path':'.md','raw_path':'.原始.md','correction_log':'.纠错记录.json','subtitle':'.srt'}
    n=1
    while True:
        stem=base+('' if n==1 else f'-{n}')
        paths={key:out/(stem+suffix) for key,suffix in suffixes.items()}
        if not any(p.exists() for p in paths.values()):break
        n+=1
    records={key:{'path':str(paths[key]),'sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()} for key,text in texts.items()}
    update={'status':'complete','final_path':str(paths['path']),'final_sha256':records['path']['sha256'],
            'review_sha256':file_hash(job/'review-state.json'),'final_turns_sha256':file_hash(job/'turns.corrected.json'),
            'export_fingerprint':fingerprint,'export_artifacts':records}
    # Persist all contents before publishing any file; retry rolls the same bundle forward.
    _write_transaction(job/'export-transaction.json',{'policy_version':2,'quality_fingerprint':acceptance['fingerprint'],'files':[{'path':str(paths[key]),'text':text} for key,text in texts.items()],
                                              'manifest_update':update})
    _recover_export(job)
    return dict(status='complete',verified=True,verification_scope='artifact_integrity',**acceptance['delivery'],**{key:str(path) for key,path in paths.items()})
