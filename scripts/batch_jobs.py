"""Single-writer, resumable batch queue. Ready media/transcripts are not final deliverables."""
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import documents as docs
import failures
import inputs
import resources
import sources


def load(file, max_items):
    file=Path(file)
    if not 1<=max_items<=100:raise ValueError('max-items must be between 1 and 100')
    if not file.is_file() or file.stat().st_size>1024*1024:raise ValueError('批量输入文件不存在或超过 1 MiB')
    text=file.read_text(encoding='utf-8-sig')
    try:values=json.loads(text) if file.suffix.lower()=='.json' else [s for s in text.splitlines() if s.strip() and not s.lstrip().startswith('#')]
    except json.JSONDecodeError:raise ValueError('批量 JSON 格式错误，请提供字符串数组') from None
    if not isinstance(values,list) or any(not isinstance(v,str) for v in values):raise ValueError('批量 JSON 必须是字符串数组')
    selected=[];seen=set()
    for row,value in enumerate(values,1):
        try:
            normalized=inputs.normalize(value);identity=inputs.identity(normalized);error=None
        except (ValueError,OSError) as exc:
            normalized=value;identity='invalid:'+value;error=sources.redact(exc)
        item_key=hashlib.sha256(identity.encode()).hexdigest()[:20]
        if item_key in seen:continue
        seen.add(item_key);selected.append(dict(key=item_key,input=normalized,row=row,input_error=error))
        if len(selected)>max_items:raise ValueError('去重后超过 max-items，请先拆分任务')
    if not selected or len(selected)>max_items:raise ValueError('批量输入为空或去重后超过 max-items，请先拆分任务')
    return selected


def execute(args,c,run,lock):
    limit=getattr(args,'limit_seconds',None)
    if limit is not None and (not math.isfinite(limit) or limit<=0):raise ValueError('limit-seconds must be positive and finite')
    selected=load(args.file,args.max_items)
    mode='prepare' if args.prepare_only else 'run'
    queue_key=docs._digest(dict(file=str(Path(args.file).resolve()),mode=mode,limit=limit))[:20]
    folder=Path(c['runtime'])/'queues'/queue_key
    if args.dry_run:
        return dict(status='batch_preview',mode=mode,count=len(selected),
                    items=[dict(key=i['key'],row=i['row'],input=sources.redact(i['input']),input_error=i['input_error']) for i in selected])
    with lock(folder),resources.scope(c):
        state_file=folder/'queue.json'
        state=docs.read_json(state_file) if state_file.exists() else dict(schema=1,mode=mode,limit_seconds=limit,items={})
        if state.get('schema')!=1 or state.get('mode')!=mode or state.get('limit_seconds')!=limit or not isinstance(state.get('items'),dict):
            raise ValueError('队列状态格式或处理模式不匹配，请保留现场并检查 queue.json')
        for item in selected:
            old=state['items'].get(item['key'],{})
            state['items'][item['key']]={**old,**item,'status':old.get('status','pending')}
        docs.write_json(state_file,state)
        budget_error=None
        for item in selected:
            record=state['items'][item['key']]
            if record['status']=='failed' and not args.retry_failed:continue
            try:resources.remaining();resources.disk(folder)
            except resources.ResourceLimit as exc:
                budget_error=sources.redact(exc);break
            record['status']='processing';docs.write_json(state_file,state)
            try:
                if item['input_error']:raise ValueError(item['input_error'])
                opts=SimpleNamespace(input=item['input'],job=None,limit_seconds=limit,
                    output=args.output,cookies=args.cookies,source_url=None,title=None,
                    language=None,speakers='auto',speaker_evidence='',prepare_only=args.prepare_only)
                result=run(opts,c)
                if result.get('status') not in ('audio_ready','review_pending','partial'):
                    raise ValueError('单项返回未知状态，不能标为就绪')
                record.update(status=result['status'],job=result['job'],error=None,
                              error_code=None,recovery_hint=None)
            except Exception as exc:
                record.update(status='failed',error=sources.redact(exc),**failures.describe(exc))
            docs.write_json(state_file,state)
        records=[state['items'][i['key']] for i in selected]
        ready=sum(i['status'] in ('audio_ready','review_pending','partial') for i in records)
        failed=sum(i['status']=='failed' for i in records)
        pending=len(records)-ready-failed
        return dict(status='batch_partial' if failed or pending else 'batch_ready',mode=mode,
                    queue=str(state_file),ready=ready,failed=failed,pending=pending,budget_error=budget_error,
                    next='每个就绪任务仍需 run/review、quality、accept-quality、finalize；失败项稍后用 --retry-failed 重试',
                    items=[{k:i.get(k) for k in ('key','row','status','job','error_code','recovery_hint')} for i in records])
