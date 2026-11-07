"""Local transcript diagnostics and bounded audio excerpts; never rewrite speech."""
import math
import re
import wave
import shutil
from pathlib import Path

import documents as docs


def inspect(job):
    job = Path(job)
    turns = docs.read_json(job / 'turns.corrected.json')
    docs._validate_turns(turns)
    manifest = docs.read_json(job / 'manifest.json')
    state = docs.read_json(job / 'review-state.json')
    docs._validate_audit(docs.read_json(job / 'turns.raw.json'), turns, state)
    issues = []
    previous = None
    for t in turns:
        reasons = []
        text = t['text'].strip()
        if docs._lexical_size(text) <= 2:
            reasons.append('short_fragment_or_response')
        if len(text) > 160 and not re.search(r'[。！？!?\n]', text):
            reasons.append('long_unpunctuated_turn')
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
