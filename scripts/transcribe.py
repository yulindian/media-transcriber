"""Local multi-platform transcription. Run with the configured Python 3.12 environment."""
import argparse
import hashlib
import json
import shutil
import sys
import time
import wave
import array
import re
from contextlib import contextmanager, ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

import sources
import engine
import documents as docs
import quality
import resources

ROOT=Path(__file__).resolve().parents[1]


@contextmanager
def job_lock(job):
    """OS-owned lock: competing writers fail; process death releases it automatically."""
    job=Path(job);job.mkdir(parents=True,exist_ok=True)
    f=(job/'.job.lock').open('a+b');locked=False
    try:
        f.seek(0,2)
        if f.tell()==0:f.write(b'\0');f.flush()
        f.seek(0)
        try:
            if sys.platform=='win32':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            locked=True
        except OSError:
            raise ValueError('该任务正在由其他进程处理，请等待其结束后再继续') from None
        yield
    finally:
        if locked:
            f.seek(0)
            if sys.platform=='win32':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_UN)
        f.close()


def output_for_job(job, explicit, c):
    m=docs.read_json(Path(job)/'manifest.json')
    return explicit or m.get('output_dir') or c['output']


def run(args,c):
    key=hashlib.sha256((args.input.strip()+'|limit='+str(args.limit_seconds)).encode()).hexdigest()[:20]
    job=Path(args.job) if args.job else Path(c['runtime'])/'jobs'/key
    with job_lock(job):
        if hasattr(docs,'recover_review'):docs.recover_review(job)
        if (job/'export-transaction.json').exists():
            raise ValueError('上次导出尚未完成，请先运行 finalize 恢复导出')
        with resources.scope(c):return _run_unlocked(args,c)


def config():
    path=ROOT/'config.json'
    return docs.read_json(path if path.exists() else ROOT/'config.example.json')


def emit(data):
    print(json.dumps(data,ensure_ascii=False,indent=2),flush=True)


def doctor(c):
    models=Path(c['runtime'])/'models'
    checks={name:(models/name).is_file() and (models/name).stat().st_size>0 for name in [engine.SENSE+'/model.int8.onnx',engine.SENSE+'/tokens.txt',engine.SEG,engine.EMBED]}
    import yt_dlp.version
    anonymous=c.get('anonymous_api') or {}
    from urllib.parse import urlsplit
    services=[]
    if isinstance(anonymous,dict) and anonymous.get('enabled'):
        for value in [anonymous]+list(anonymous.get('fallbacks') or []):
            if isinstance(value,dict):services.append(urlsplit(value.get('endpoint','')).hostname)
    curl=shutil.which('curl.exe') or shutil.which('curl')
    curl_required=isinstance(anonymous,dict) and anonymous.get('enabled') is True
    ready=all(checks.values()) and shutil.which('ffmpeg') and (curl or not curl_required)
    return {'status':'ready' if ready else 'needs_setup','python':sys.executable,'ffmpeg':shutil.which('ffmpeg'),'models':checks,'yt_dlp':yt_dlp.version.__version__,'output':c['output'],'resolvers':list(c.get('resolvers',{})),'anonymous_services':services,'curl':curl,'asr_language':c.get('asr_language','zh')}


def _valid_cached_wav(wav, manifest):
    if not wav.is_file() or manifest.get('audio_ready_sha256') != docs.file_hash(wav):
        return False
    try:
        with wave.open(str(wav), 'rb') as stream:
            return (stream.getframerate() == 16000 and stream.getnchannels() == 1
                    and stream.getsampwidth() == 2 and stream.getnframes() > 0)
    except (OSError, EOFError, wave.Error):
        return False


def _checked_refresh(previous, refreshed, input_text):
    if (not isinstance(refreshed, dict) or refreshed.get('local_path')
            or refreshed.get('source_url') != input_text
            or (previous.get('platform') and previous.get('platform') != refreshed.get('platform'))):
        raise ValueError('刷新来源与原输入或平台不一致，拒绝下载其他作品')
    old_id, new_id = str(previous.get('id') or ''), str(refreshed.get('id') or '')
    if old_id and new_id and old_id != new_id:
        def opaque(identity):
            return not identity.isdigit() and bool(re.fullmatch(r'[a-f0-9]{16}|[a-f0-9]{20}|[a-f0-9]{64}', identity))
        title_matches = (isinstance(previous.get('title'), str) and previous['title'].strip()
                         and previous['title'].strip() == str(refreshed.get('title') or '').strip())
        if not (opaque(old_id) or opaque(new_id)) or not title_matches:
            raise ValueError('刷新来源的作品ID发生变化，拒绝替换原作品')
        if not opaque(old_id):
            refreshed = {**refreshed, 'id': old_id}
    return refreshed


def _run_unlocked(args,c):
    input_text=args.input.strip();limit=args.limit_seconds
    key=hashlib.sha256((input_text+'|limit='+str(limit)).encode()).hexdigest()[:20]
    job=Path(args.job) if args.job else Path(c['runtime'])/'jobs'/key
    job.mkdir(parents=True,exist_ok=True);manifest_file=job/'manifest.json'
    if manifest_file.exists():
        m=docs.read_json(manifest_file)
        if m['input']!=input_text or m.get('limit_seconds')!=limit:
            raise ValueError('该工作目录属于不同输入或试跑长度，请使用新的 --job')
    else:
        m={'version':1,'input':input_text,'created_at':datetime.now(timezone(timedelta(hours=8))).isoformat(),'limit_seconds':limit,'status':'pending'}
    if args.output:m['output_dir']=str(Path(args.output).resolve())
    previous_review_input=m.get('review_input_sha256')
    previous_audio=m.get('audio_sha256')
    m.update(status='pending',coverage_complete=False)
    docs.write_json(manifest_file,m)
    try:
        t0=time.monotonic()
        info_file=job/'source.json'
        if info_file.exists():info=docs.read_json(info_file)
        else:
            info=sources.metadata(input_text,args.cookies,c)
            if args.source_url:info['source_url']=sources.http_url(args.source_url)
            if args.title:info['title']=args.title
            docs.write_json(info_file,info)
        if limit is not None:resources.audio(limit,parallel=True)
        elif info.get('duration') is not None:resources.audio(float(info['duration']),parallel=True)
        if info.get('local_path') and Path(info['local_path']).stat().st_size>resources.policy()['max_download_bytes']:
            raise resources.ResourceLimit('本地媒体超过 max_download_bytes')
        m.update(status='acquiring');docs.write_json(manifest_file,m)
        wav=job/'audio.wav';fingerprint=None
        if info.get('local_path'):fingerprint=docs.file_hash(info['local_path'])
        converted=_valid_cached_wav(wav,m)
        if not converted or (fingerprint and m.get('local_input_sha256')!=fingerprint):
            first_source=info
            m['download_attempts']=[]
            def acquire_route(descriptor):
                resources.remaining();resources.disk(job)
                attempt={'route':descriptor.get('acquisition','unknown'),'status':'failed'}
                m['download_attempts'].append(attempt)
                try:
                    result=sources.acquire(descriptor,job,args.cookies)
                    attempt['status']='success'
                    return result
                finally:
                    docs.write_json(manifest_file,m)
            try:
                media=acquire_route(info)
            except resources.ResourceLimit:raise
            except ValueError:
                if info.get('local_path'):
                    raise
                # Expiring signed URLs are refreshed only once during this invocation.
                m['source_refreshes']=m.get('source_refreshes',0)+1
                docs.write_json(manifest_file,m)
                fresh=sources.metadata(input_text,args.cookies,c)
                info=_checked_refresh(info,fresh,input_text)
                if args.source_url:info['source_url']=sources.http_url(args.source_url)
                if args.title:info['title']=args.title
                docs.write_json(info_file,info)
                try:
                    media=acquire_route(info)
                except resources.ResourceLimit:raise
                except ValueError:
                    import anonymous_sources as anonymous
                    native_routes={'yt-dlp','douyin_public_app'}
                    provider=sources.platform(input_text)
                    if (args.cookies or first_source.get('acquisition') not in native_routes
                            or info.get('acquisition') not in native_routes
                            or not anonymous.api_enabled(provider,c)):
                        raise
                    # Native metadata can succeed while the actual stream returns 403.
                    # One API chain is permitted here; an API-origin download never loops back.
                    m['source_api_fallbacks']=m.get('source_api_fallbacks',0)+1
                    docs.write_json(manifest_file,m)
                    fallback=anonymous.api_metadata(input_text,provider,c)
                    info=_checked_refresh(info,fallback,input_text)
                    if args.source_url:info['source_url']=sources.http_url(args.source_url)
                    if args.title:info['title']=args.title
                    docs.write_json(info_file,info)
                    media=acquire_route(info)
            resources.disk(job)
            sources.convert(media,wav,limit)
            m['audio_ready_sha256']=docs.file_hash(wav)
            docs.write_json(manifest_file,m)
        m['local_input_sha256']=fingerprint
        # Read only the header here; ASR and cold diarization load samples as needed.
        with wave.open(str(wav),'rb') as f:
            if f.getframerate()!=16000 or f.getnchannels()!=1 or f.getsampwidth()!=2 or not f.getnframes():
                raise ValueError('识别输入不是有效的16kHz单声道PCM16音频')
            actual=f.getnframes()/16000
            resources.audio(actual,parallel=True)
            digest=m['audio_ready_sha256']
            if m.get('signal_checked_sha256')!=digest:
                nonzero=False
                while True:
                    frames=f.readframes(65536)
                    if not frames:break
                    if any(array.array('h',frames)):nonzero=True;break
                m.update(signal_checked_sha256=digest,audio_has_nonzero_signal=nonzero)
                docs.write_json(manifest_file,m)
        if not m.get('audio_has_nonzero_signal'):
            raise ValueError('音轨为全静音，不能生成语音逐字稿')
        # Legacy caches used a fixed Chinese recognizer. Keep that reviewed work
        # unless the caller explicitly chooses a new language for this job.
        language=(getattr(args,'language',None) or m.get('asr_language')
                  or ('zh' if (job/'asr.json').exists() else c.get('asr_language','zh')))
        if language not in ('auto','zh','en','yue','ja','ko'):raise ValueError('不支持的识别语言')
        m['asr_language']=language
        expected=info.get('duration')
        coverage=limit is None
        if not limit and expected is not None and actual<float(expected)-max(2,float(expected)*.001):
            raise ValueError('下载音轨长度明显短于节目时长，拒绝标记完整')
        m.update(audio_duration=actual,coverage_complete=False,status='transcribing',audio_sha256=digest)
        docs.write_json(manifest_file,m)
        count=None if args.speakers=='auto' else int(args.speakers)
        # One ASR stage and one speaker stage share the local CPU. ASR JSONL is resumable.
        with ThreadPoolExecutor(max_workers=2) as pool:
            if language=='zh':a=resources.submit(pool,engine.asr,job,Path(c['runtime'])/'models',c.get('threads',4))
            else:a=resources.submit(pool,engine.asr,job,Path(c['runtime'])/'models',c.get('threads',4),language=language)
            d=resources.submit(pool,engine.diarize,job,Path(c['runtime'])/'models',count,c.get('segmentation_shift',.5))
            asr=a.result()
            try:speaker=d.result()
            except resources.ResourceLimit:raise
            except Exception as exc:
                speaker={'segments':[],'report':{'count':None,'count_mode':'unavailable','error':sources.redact(exc)}}
        resources.remaining()
        chunks=asr['chunks']
        if not chunks or abs(chunks[0]['start'])>.01 or abs(chunks[-1]['end']-actual)>.01 or any(abs(x['end']-y['start'])>.01 for x,y in zip(chunks,chunks[1:])):
            raise ValueError('ASR 音频区间不连续或未覆盖全部输入')
        turns=engine.combine(asr,speaker)
        if not turns:raise ValueError('没有识别到可用正文，不能生成完整文稿')
        existed=(job/'turns.raw.json').exists()
        try:old=docs.read_json(job/'turns.raw.json') if existed else None
        except (OSError,ValueError):old=None
        pair_valid=False
        review_input=docs._digest({'audio':digest,'turns':turns})
        same_review_input=(previous_review_input==review_input if previous_review_input is not None
                           else previous_audio in (None,digest))
        if old==turns and same_review_input:
            try:
                docs._validate_audit(old,docs.read_json(job/'turns.corrected.json'),docs.read_json(job/'review-state.json'))
                pair_valid=True
            except (OSError,ValueError,TypeError,KeyError):pass
        if old!=turns or not pair_valid:docs.reset_review(job,turns)
        warnings=[]
        if existed and old==turns and not pair_valid:
            warnings.append('恢复了不一致的校对状态：旧文件已备份，正文需重新校对')
        if speaker['report'].get('count_mode')=='unavailable':
            warnings.append('说话人处理不可用，正文保留但归属为未确定：'+speaker['report'].get('error','未知原因'))
        if speaker['report'].get('limited_evidence'):
            warnings.append('存在语音证据不足的说话人候选，自动人数采用保守估计，短发言归属需核对')
        m['warnings']=warnings
        m.update(status='review_pending' if coverage else 'partial',coverage_complete=coverage,
                 coverage_basis=('partial_audio' if not coverage else 'source_duration_checked' if expected is not None else 'acquired_audio_only'),
                 review_input_sha256=review_input,
                 speaker_report={**speaker['report'],'evidence':args.speaker_evidence if count else ''},turn_count=len(turns),elapsed_last_run=time.monotonic()-t0,asr_elapsed=asr['elapsed_this_run'])
        docs.write_json(manifest_file,m);docs.draft(job)
        state=docs.read_json(job/'review-state.json')
        already_reviewed=set(state['reviewed_ids'])=={t['id'] for t in turns}
        return {'status':m['status'],'job':str(job),'raw_markdown':str(job/'原始识别稿.md'),'turn_count':len(turns),'audio_seconds':actual,'speaker_report':m['speaker_report'],'warnings':warnings,'review_already_complete':already_reviewed,'next':'quality → accept-quality → finalize' if already_reviewed else 'review --next → apply-edits → quality → accept-quality → finalize','output':m.get('output_dir') or c['output']}
    except Exception as exc:
        m.update(status='failed',coverage_complete=False,error=sources.redact(exc));docs.write_json(manifest_file,m)
        if isinstance(exc,resources.ResourceLimit):raise resources.ResourceLimit(m['error']) from None
        raise ValueError(m['error']) from None


def review(args):
    job=Path(args.job)
    with job_lock(job):
        if hasattr(docs,'recover_review'):docs.recover_review(job)
        return _review_unlocked(args)


def _review_unlocked(args):
    job=Path(args.job);current=docs.read_json(job/'turns.corrected.json');state=docs.read_json(job/'review-state.json')
    docs._validate_audit(docs.read_json(job/'turns.raw.json'),current,state)
    count=min(max(args.count,1),60)
    if args.offset<0:raise ValueError('offset must be nonnegative')
    budget=getattr(args,'max_chars',6000)
    if budget<=0:raise ValueError('max-chars must be positive')
    reviewed=set(state['reviewed_ids']);selected=[];chars=0;last=args.offset-1
    for i in range(args.offset,len(current)):
        t=current[i]
        if getattr(args,'next',False) and t['id'] in reviewed:continue
        if selected and (len(selected)>=count or chars+len(t['text'])>budget):break
        selected.append(t);chars+=len(t['text']);last=i
    info=docs.read_json(job/'source.json');m=docs.read_json(job/'manifest.json')
    if not getattr(args,'verbose',False):
        info={k:info[k] for k in ('id','title','author','podcast','platform','source_url','description') if k in info}
        m={k:m[k] for k in ('status','audio_duration','coverage_complete','speaker_report','warnings') if k in m}
    return {'source':info,'manifest':m,'total_turns':len(current),'offset':args.offset,'next_offset':min(last+1,len(current)) if selected else len(current),'already_reviewed':len(reviewed),'selected_chars':chars,'turns':selected}


def edit(args):
    with job_lock(args.job):
        if hasattr(docs,'recover_review'):docs.recover_review(args.job)
        return _edit_unlocked(args)


def _edit_unlocked(args):
    job=Path(args.job);current=docs.read_json(job/'turns.corrected.json');state=docs.read_json(job/'review-state.json')
    payload=docs.read_json(args.input)
    report=docs.preflight_review(current,payload,job)
    if getattr(args,'check',False):return dict(status='preflight',**report)
    if not report['valid']:raise ValueError(json.dumps(report,ensure_ascii=False))
    new,edits,ids=docs.apply_review(current,payload,job)
    state['reviewed_ids']=sorted(set(state['reviewed_ids'])|set(ids));state['edits'].extend(edits)
    docs.save_review(job,new,state)
    return {'status':'review_saved','reviewed':len(state['reviewed_ids']),'total':len(new),'edits':len(state['edits'])}


def finalize_job(args,c):
    with job_lock(args.job):
        if hasattr(docs,'recover_review'):docs.recover_review(args.job)
        output=output_for_job(args.job,args.output,c)
        directories={Path(output).resolve()}
        journal=Path(args.job)/'export-transaction.json'
        if journal.exists():
            recovery=docs._read_transaction(journal)
            directories.update(Path(record['path']).parent.resolve() for record in recovery['files'])
        directories.discard(Path(args.job).resolve())
        with ExitStack() as stack:
            for directory in sorted(directories,key=lambda p:str(p).casefold()):stack.enter_context(job_lock(directory))
            return docs.finalize(args.job,output)


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('doctor',help='Check runtime, models and configuration')
    pr=sub.add_parser('probe',help='Inspect one URL without downloading media');pr.add_argument('url');pr.add_argument('--cookies')
    r=sub.add_parser('run',help='Acquire, normalize, transcribe and label speakers; resumes cached stages')
    r.add_argument('input',help='Single platform URL, direct media URL, or local media path')
    r.add_argument('--source-url');r.add_argument('--title');r.add_argument('--job');r.add_argument('--output');r.add_argument('--cookies')
    r.add_argument('--language',choices=['auto','zh','en','yue','ja','ko'],help='ASR language; new tasks use config, existing tasks keep their recorded language')
    r.add_argument('--speakers',default='auto',choices=['auto']+[str(i) for i in range(1,13)])
    r.add_argument('--speaker-evidence',default='',help='Evidence for a specified count; otherwise describe it as an assumption')
    r.add_argument('--limit-seconds',type=float,help='Explicit partial test; never exports as a complete final')
    v=sub.add_parser('review',help='Read one batch of transcript turns for conservative correction');v.add_argument('job');v.add_argument('--offset',type=int,default=0);v.add_argument('--count',type=int,default=30)
    v.add_argument('--next',action='store_true',help='Return only not-yet-reviewed turns')
    v.add_argument('--max-chars',type=int,default=6000,help='Bound batch text without truncating a turn')
    v.add_argument('--verbose',action='store_true',help='Include detailed source and manifest metadata')
    e=sub.add_parser('apply-edits',help='Apply auditable edits and record reviewed turn IDs');e.add_argument('job');e.add_argument('--input',required=True)
    e.add_argument('--check',action='store_true',help='Validate all edits without saving or advancing review')
    q=sub.add_parser('quality',help='Read local quality candidates; does not change review state');q.add_argument('job');q.add_argument('--offset',type=int,default=0);q.add_argument('--count',type=int,default=30)
    q=sub.add_parser('accept-quality',help='Accept current quality snapshot with explicit retained-issue reasons');q.add_argument('job');q.add_argument('--input',required=True)
    q=sub.add_parser('clip',help='Extract a local WAV excerpt for one turn');q.add_argument('job');q.add_argument('--turn-id',required=True);q.add_argument('--context',type=float,default=3)
    q=sub.add_parser('recheck',help='Re-recognize a bounded excerpt into a separate cache without applying edits');q.add_argument('job');q.add_argument('--turn-id',required=True);q.add_argument('--context',type=float,default=3)
    f=sub.add_parser('finalize',help='Export complete reviewed Markdown, raw copy, correction log and SRT');f.add_argument('job');f.add_argument('--output')
    args=p.parse_args()
    try:
        c=config()
        if args.command=='doctor':result=doctor(c)
        elif args.command=='probe':
            result=sources.metadata(args.url,args.cookies,c)
            result.pop('media_url',None)
            if 'resolved_url' in result:result['resolved_url']=sources.redact(result['resolved_url'])
        elif args.command=='run':
            if args.limit_seconds is not None and args.limit_seconds<=0:raise ValueError('limit-seconds must be positive')
            result=run(args,c)
        elif args.command=='review':result=review(args)
        elif args.command=='apply-edits':result=edit(args)
        elif args.command=='quality':
            if args.offset<0 or not 1<=args.count<=100:raise ValueError('offset >= 0 and count between 1 and 100 required')
            with job_lock(args.job):
                docs.recover_review(args.job)
                result=quality.check(args.job)
            candidates=result['issues'];result['total_candidates']=len(candidates)
            result['issues']=candidates[args.offset:args.offset+args.count]
            result['next_offset']=min(args.offset+args.count,len(candidates))
        elif args.command=='accept-quality':
            with job_lock(args.job):
                docs.recover_review(args.job)
                result=quality.accept(args.job,docs.read_json(args.input))
        elif args.command=='clip':
            with job_lock(args.job):
                docs.recover_review(args.job)
                result=quality.clip(args.job,args.turn_id,args.context)
        elif args.command=='recheck':
            with job_lock(args.job):
                docs.recover_review(args.job)
                with resources.scope(c):result=quality.recheck(args.job,args.turn_id,c,args.context)
        else:result=finalize_job(args,c)
        emit(result)
        if result.get('status')=='preflight' and not result.get('valid'):sys.exit(2)
    except Exception as exc:
        emit({'status':'error','error_code':'resource_limit' if isinstance(exc,resources.ResourceLimit) else 'workflow_error','message':sources.redact(exc)});sys.exit(1)


if __name__=='__main__':main()
