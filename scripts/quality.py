"""Local transcript diagnostics and bounded audio excerpts; never rewrite speech."""
import math
import re
import wave
import shutil
from pathlib import Path

import documents as docs
import resources


def inspect(job):
    job = Path(job)
    turns = docs.read_json(job / 'turns.corrected.json')
    docs._validate_turns(turns)
    manifest = docs.read_json(job / 'manifest.json')
    state = docs.read_json(job / 'review-state.json')
    docs._validate_audit(docs.read_json(job / 'turns.raw.json'), turns, state)
    issues = []
    previous = None
    by_id={t['id']:t for t in turns};unverified_entities=set()
    for edit in state['edits']:
        if docs.entities(edit['before'])!=docs.entities(edit['after']):
            try:docs._entity_evidence(edit,{**by_id[edit['turn_id']],'text':edit['before']},job)
            except (ValueError,OSError,TypeError):unverified_entities.add(edit['turn_id'])
    for t in turns:
        reasons = []
        if t['id'] in unverified_entities:reasons.append('unverified_historical_entity_change')
        text = t['text'].strip()
        if docs._lexical_size(text) <= 2:
            reasons.append('short_fragment_or_response')
        if max(map(len,re.split(r'[。！？!?；;\n]',text)),default=0)>160:
            reasons.append('long_unpunctuated_turn')
        if round(t['end']*1000)<=round(t['start']*1000):reasons.append('zero_duration_subtitle')
        if any(x in t['speaker'] for x in ('待核对', '未确定')):
            reasons.append('speaker_uncertain')
        if '/' in t['speaker']:
            reasons.append('multiple_speaker_candidates')
        if previous:
            if t['start'] < previous['start']:
                reasons.append('time_order_error')
            if t['start'] < previous['end'] - .1:
                reasons.append('overlapping_time_ranges')
            if t['speaker'] != previous['speaker'] and t['end'] - t['start'] < 1:
                reasons.append('brief_speaker_switch')
        if reasons:
            issues.append(dict(turn_id=t['id'], start=t['start'], end=t['end'],
                               speaker=t['speaker'], reasons=reasons))
        previous = t
    counts = {}
    for issue in issues:
        for reason in issue['reasons']:
            counts[reason] = counts.get(reason, 0) + 1
    duration = manifest.get('audio_duration')
    return dict(total_turns=len(turns), reviewed_turns=len(state['reviewed_ids']),
                text_review_complete=set(state['reviewed_ids']) == {t['id'] for t in turns},
                audio_manually_verified=False, speaker_accuracy_verified=False,
                first_speech=turns[0]['start'] if turns else None,
                last_speech=max((t['end'] for t in turns), default=None),
                audio_seconds=duration, counts=counts, issues=issues,
                note='Heuristic candidates only; silence, short responses and overlap may be valid. No automatic corrections.')


def _fingerprint(job):
    job=Path(job);manifest=docs.read_json(job/'manifest.json')
    evidence_files={};root=(job/'review-clips').resolve()
    for edit in docs.read_json(job/'review-state.json')['edits']:
        evidence=edit.get('entity_review')
        if isinstance(evidence,dict) and isinstance(evidence.get('clip_path'),str):
            path=Path(evidence['clip_path']).resolve()
            if path.is_relative_to(root):
                for file in (path,path.with_suffix('.json')):
                    evidence_files[str(file.relative_to(root))]=docs.file_hash(file) if file.is_file() else None
    return docs._digest({'version':2,'files':{name:docs.file_hash(job/name) for name in
                        ('source.json','turns.raw.json','turns.corrected.json','review-state.json')},
                        'audio':docs.file_hash(job/'audio.wav') if (job/'audio.wav').exists() else None,
                        'entity_evidence_files':evidence_files,
                        'manifest':{k:manifest.get(k) for k in ('audio_duration','coverage_complete','limit_seconds','asr_language','speaker_report','review_input_sha256','coverage_basis')}})


def check(job):
    report=inspect(job)
    record={'fingerprint':_fingerprint(job),'counts':report['counts'],'issues':report['issues'],
            'text_review_complete':report['text_review_complete']}
    docs._write_transaction(Path(job)/'quality-check.json',record)
    return dict(report,snapshot_fingerprint=record['fingerprint'])


def _current_check(job):
    file=Path(job)/'quality-check.json'
    if not file.exists():raise ValueError('请先运行 quality，再完成 accept-quality 后导出')
    record=docs._read_transaction(file)
    if record.get('fingerprint')!=_fingerprint(job):raise ValueError('质量检查已过期，请重新运行 quality 和 accept-quality')
    return record


def accept(job, payload):
    record=_current_check(job)
    if not record['text_review_complete']:raise ValueError('仍有正文未校对，不能验收')
    if not isinstance(payload,dict) or payload.get('snapshot_fingerprint')!=record['fingerprint']:
        raise ValueError('验收必须引用当前 quality 返回的 snapshot_fingerprint')
    decisions=payload.get('accepted_issues')
    if not isinstance(decisions,list):raise ValueError('accepted_issues must be a list')
    candidates={issue['turn_id'] for issue in record['issues']};seen=set()
    for decision in decisions:
        if (not isinstance(decision,dict) or not isinstance(decision.get('turn_id'),str)
                or decision['turn_id'] not in candidates or decision['turn_id'] in seen
                or not isinstance(decision.get('reason'),str) or not decision['reason'].strip()):
            raise ValueError('每个保留疑点需要唯一的候选 turn_id 和接受理由')
        seen.add(decision['turn_id'])
    if seen!=candidates:raise ValueError('尚有未处理的质量疑点；修正后重新检查，或逐段说明保留理由')
    # Zero-length subtitles are a hard structural error, not an acceptable doubt.
    if any('zero_duration_subtitle' in item['reasons'] for item in record['issues']):
        raise ValueError('零时长字幕不能验收，请重新处理时间片')
    result=dict(record,accepted_issues=decisions,
                delivery={'quality_status':'accepted_with_disclosed_issues' if decisions else 'accepted_no_detected_issues',
                          'accepted_issue_turns':len(decisions),'audio_review_status':'not_verified','speaker_review_status':'not_verified'})
    docs._write_transaction(Path(job)/'quality-acceptance.json',result)
    return dict(status='quality_accepted',**result['delivery'])


def require_accepted(job):
    record=_current_check(job)
    file=Path(job)/'quality-acceptance.json'
    if not file.exists():raise ValueError('缺少质量验收，请执行 accept-quality')
    result=docs._read_transaction(file)
    if any(result.get(k)!=record[k] for k in ('fingerprint','counts','issues','text_review_complete')):
        raise ValueError('质量验收已过期，请重新检查并验收')
    # Validate the decisions and derived grade again instead of trusting stored flags.
    payload={'snapshot_fingerprint':record['fingerprint'],'accepted_issues':result.get('accepted_issues')}
    expected_ids={x['turn_id'] for x in record['issues']}
    decisions=payload['accepted_issues']
    if (not record['text_review_complete'] or not isinstance(decisions,list)
            or any(not isinstance(d,dict) or not isinstance(d.get('turn_id'),str)
                   or not isinstance(d.get('reason'),str) or not d['reason'].strip() for d in decisions)
            or len(decisions)!=len(expected_ids) or {d['turn_id'] for d in decisions}!=expected_ids
            or any('zero_duration_subtitle' in i['reasons'] for i in record['issues'])):
        raise ValueError('Invalid quality acceptance decisions')
    expected={'quality_status':'accepted_with_disclosed_issues' if decisions else 'accepted_no_detected_issues',
              'accepted_issue_turns':len(decisions),'audio_review_status':'not_verified','speaker_review_status':'not_verified'}
    if result.get('delivery')!=expected:raise ValueError('Invalid quality delivery grade')
    return result


def clip(job, turn_id, context=3):
    """Copy PCM frames directly, preserving channels and sample format."""
    if not math.isfinite(context) or not 0 <= context <= 30:
        raise ValueError('context must be between 0 and 30 seconds')
    job = Path(job)
    turns = docs.read_json(job / 'turns.corrected.json')
    docs._validate_turns(turns)
    t = next((t for t in turns if t['id'] == turn_id), None)
    if t is None:
        raise ValueError('Unknown turn ID')
    with wave.open(str(job / 'audio.wav'), 'rb') as src:
        rate = src.getframerate()
        start = max(0, int((t['start'] - context) * rate))
        end = min(src.getnframes(), int((t['end'] + context) * rate))
        if end <= start or end - start > rate * 120:
            raise ValueError('Clip must contain audio and be at most 120 seconds')
        src.setpos(start)
        data = src.readframes(end - start)
        out = job / 'review-clips'
        out.mkdir(exist_ok=True)
        path = out / (docs.filename(turn_id) + '.wav')
        with wave.open(str(path), 'wb') as dst:
            dst.setparams(src.getparams())
            dst.writeframes(data)
    docs.write_json(path.with_suffix('.json'),{'turn_id':turn_id,'turn_sha256':docs._digest(t),
                    'clip_sha256':docs.file_hash(path),'audio_sha256':docs.file_hash(job/'audio.wav'),
                    'start':start/rate,'end':end/rate})
    return dict(path=str(path), start=start / rate, end=end / rate,
                turn_id=turn_id, text=t['text'], speaker=t['speaker'],
                note='Extracted only; listening or re-recognition is not yet verified.')


def recheck(job, turn_id, config, context=3):
    """Re-decode a bounded context in a separate cache; never apply its wording."""
    import engine
    excerpt=clip(job,turn_id,context)
    cache=Path(job)/'review-clips'/('asr-'+docs.filename(turn_id))
    cache.mkdir(exist_ok=True)
    audio=cache/'audio.wav'
    if not audio.exists() or docs.file_hash(audio)!=docs.file_hash(excerpt['path']):
        shutil.copyfile(excerpt['path'],audio)
    manifest=docs.read_json(Path(job)/'manifest.json')
    language=manifest.get('asr_language',config.get('asr_language','auto'))
    result=engine.asr(cache,Path(config['runtime'])/'models',config.get('threads',4),language=language)
    return dict(**{k:v for k,v in excerpt.items() if k!='note'},
                candidate_text=''.join(c['text'] for c in result['chunks']),
                cache=str(cache),
                note='Same-model candidate includes context; not independent verification, not auto-applied, no speaker validation.')
