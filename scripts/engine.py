"""CPU ASR and speaker diarization, with reusable stage files and estimates."""
import collections
import bisect
import heapq
import hashlib
import json
import re
import subprocess
import time
import wave
import resources
from pathlib import Path

import numpy as np
import sherpa_onnx
from documents import _write_text, file_hash, read_json, write_json

SENSE='sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09'
EMBED='3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx'
SEG='sherpa-onnx-pyannote-segmentation-3-0/model.onnx'


def audio(path):
    with wave.open(str(path),'rb') as f:
        if f.getframerate()!=16000 or f.getnchannels()!=1 or f.getsampwidth()!=2:raise ValueError('Expected 16kHz mono PCM16 WAV')
        return np.frombuffer(f.readframes(f.getnframes()),dtype=np.int16).astype(np.float32)/32768


def audio_duration(path):
    """Read the WAV header without allocating the waveform on a cache hit."""
    with wave.open(str(path),'rb') as f:
        if f.getframerate()!=16000 or f.getnchannels()!=1 or f.getsampwidth()!=2:
            raise ValueError('Expected 16kHz mono PCM16 WAV')
        resources.audio(f.getnframes()/16000);resources.remaining()
        return f.getnframes()/16000


def _number(value):
    return isinstance(value,(int,float,np.integer,np.floating)) and not isinstance(value,(bool,np.bool_)) and np.isfinite(value)


def _validate_chunk(chunk, expected=None, duration=None):
    if not isinstance(chunk,dict):raise ValueError('Invalid ASR chunk')
    start=chunk.get('start');end=chunk.get('end')
    if not _number(start) or not _number(end) or start<0 or end<=start:
        raise ValueError('Invalid ASR chunk interval')
    if expected is not None and abs(start-expected)>.01:raise ValueError('ASR chunk coverage is discontinuous')
    if duration is not None and end>duration+.01:raise ValueError('ASR chunk exceeds audio duration')
    tokens=chunk.get('tokens');ts=chunk.get('timestamps');text=chunk.get('text')
    if not isinstance(tokens,list) or not all(isinstance(t,str) for t in tokens) or not isinstance(ts,list) or not isinstance(text,str):
        raise ValueError('Invalid ASR token/time/text structure')
    if len(tokens)!=len(ts):raise ValueError('Cached ASR token/time count mismatch')
    if re.sub(r'\s','',''.join(tokens))!=re.sub(r'\s','',text):raise ValueError('Cached ASR text/token mismatch')
    if any(not _number(t) or t<start-.05 or t>end+.5 for t in ts) or any(x>y for x,y in zip(ts,ts[1:])):
        raise ValueError('Cached ASR timestamps are invalid')


def _validate_asr(result, complete=False):
    if not isinstance(result,dict) or not isinstance(result.get('chunks'),list):raise ValueError('Invalid ASR chunks')
    duration=result.get('duration')
    if duration is not None and (not _number(duration) or duration<0):raise ValueError('Invalid ASR duration')
    end=0
    for chunk in result['chunks']:
        _validate_chunk(chunk,end,duration);end=chunk['end']
    if complete and (duration is None or abs(end-duration)>.01):raise ValueError('Cached ASR coverage is incomplete')


def _validate_regions(regions, duration=None, labeled=False):
    if not isinstance(regions,list):raise ValueError('Invalid speaker regions')
    for region in regions:
        if not isinstance(region,dict):raise ValueError('Invalid speaker region')
        start=region.get('start');end=region.get('end')
        if not _number(start) or not _number(end) or start<0 or end<=start or (duration is not None and end>duration+.05):
            raise ValueError('Invalid speaker region interval')
        if labeled:
            speaker=region.get('speaker');margin=region.get('margin',0)
            if not isinstance(speaker,(int,np.integer)) or isinstance(speaker,(bool,np.bool_)) or speaker<0 or not _number(margin):
                raise ValueError('Invalid speaker label or margin')


def _validate_features(features, durations):
    x=np.asarray(features,dtype=np.float64);durations=np.asarray(durations,dtype=np.float64)
    if x.ndim!=2 or not x.shape[0] or not x.shape[1] or not np.isfinite(x).all() or np.any(np.linalg.norm(x,axis=1)<=1e-8):
        raise ValueError('Invalid speaker embeddings')
    if durations.ndim!=1 or len(durations)!=len(x) or not np.isfinite(durations).all() or np.any(durations<=0):
        raise ValueError('Speaker feature/duration count or values are invalid')
    return x,durations


def signature(job, kind, settings):
    return hashlib.sha256(json.dumps({'audio':file_hash(Path(job)/'audio.wav'),'kind':kind,'settings':settings},sort_keys=True).encode()).hexdigest()


def spherical(x, weights, k):
    # Deterministic farthest-point initialization on reliable speech only.
    mean=np.average(x,axis=0,weights=weights);mean/=max(np.linalg.norm(mean),1e-8)
    centers=[x[np.argmax(x@mean)]]
    for _ in range(1,k):
        distance=1-np.max(x@np.asarray(centers).T,axis=1)
        centers.append(x[np.argmax(distance)])
    centers=np.asarray(centers)
    for _ in range(40):
        labels=np.argmax(x@centers.T,axis=1);new=[]
        for i in range(k):
            mask=labels==i
            if not mask.any():new.append(centers[i]);continue
            c=(x[mask]*weights[mask,None]).sum(0);new.append(c/max(np.linalg.norm(c),1e-8))
        new=np.asarray(new)
        if np.max(np.abs(new-centers))<1e-5:centers=new;break
        centers=new
    return np.argmax(x@centers.T,axis=1),centers


def cluster_speakers(features, durations, count):
    x,durations=_validate_features(features,durations)
    if count is not None and (not isinstance(count,int) or isinstance(count,bool) or count<1):raise ValueError('Speaker count must be a positive integer')
    x=x/np.maximum(np.linalg.norm(x,axis=1,keepdims=True),1e-8)
    reliable=durations>=.3 if count is None else durations>=2
    if reliable.sum()<max(4,count or 1):reliable=durations>=.3
    if not reliable.any():reliable=np.ones(len(x),dtype=bool)
    fit=x[reliable];weights=np.minimum(durations[reliable],15)
    weights=np.maximum(weights,.1)
    estimate_scores={};rejected_evidence=[];limited_evidence=False
    if count is None:
        mean=np.average(fit,axis=0,weights=weights);mean/=max(np.linalg.norm(mean),1e-8)
        # A minority speaker must not disappear behind the majority's median.
        dispersion=float(np.quantile(1-fit@mean,.9))
        count=1
        if len(fit)>=4 and dispersion>.08:
            indices=np.linspace(0,len(fit)-1,min(len(fit),240),dtype=int)
            sample=fit[indices];distance=np.clip(1-sample@sample.T,0,2)
            best=.30
            for k in range(2,min(6,len(fit)//2)+1):
                labels,centers=spherical(fit,weights,k);ls=labels[indices];sil=[]
                evidence=[float(durations[reliable][labels==speaker].sum()) for speaker in range(k)]
                for i in range(len(sample)):
                    own=ls==ls[i];own[i]=False
                    if not own.any():sil.append(0);continue
                    intra=distance[i,own].mean()
                    other=[distance[i,ls==j].mean() for j in set(ls) if j!=ls[i]]
                    if not other:sil.append(0);continue
                    inter=min(other);sil.append((inter-intra)/max(inter,intra,1e-8))
                score=float(np.mean(sil))-.04*(k-2)
                estimate_scores[k]=score
                if any(seconds<2.0 for seconds in evidence):
                    rejected_evidence.append({'count':k,'speech_seconds':evidence,'score':score})
                    if score>best:limited_evidence=True
                    continue
                if score>best:best=score;count=k
        mode='estimated'
    else:mode='specified'
    if count>len(fit):raise ValueError('有效语音片段不足以支持指定人数')
    _,centers=spherical(fit,weights,count)
    scores=x@centers.T;labels=np.argmax(scores,axis=1)
    if count==1:margin=np.ones(len(x))
    else:
        top=np.sort(scores,axis=1)[:,-2:];margin=top[:,1]-top[:,0]
    return labels,{'count':int(count),'count_mode':mode,'max_estimated_count':6,'estimate_scores':estimate_scores,'method':'duration_weighted_spherical_kmeans','margins':margin.tolist(),'accuracy_verified':False,'limited_evidence':limited_evidence,'minimum_cluster_speech_seconds':2.0,'rejected_evidence':rejected_evidence}


def asr(job, models, threads=4, language='zh'):
    if not isinstance(language,str) or language not in ('auto','zh','en','yue','ja','ko'):
        raise ValueError('Unsupported ASR language; use auto, zh, en, yue, ja or ko')
    job=Path(job);models=Path(models)
    settings={'model':file_hash(models/SENSE/'model.int8.onnx'),'tokens':file_hash(models/SENSE/'tokens.txt')}
    # Preserve the exact original Chinese signature for existing completed jobs.
    if language!='zh':settings['language']=language
    sig=signature(job,'asr-v1',settings)
    done=job/'asr.json'
    if done.exists():
        cached=read_json(done)
        if not isinstance(cached,dict):raise ValueError('Invalid ASR cache')
        if cached.get('signature')==sig:
            _validate_asr(cached,complete=True)
            if abs(cached['duration']-audio_duration(job/'audio.wav'))>.01:raise ValueError('Cached ASR audio duration mismatch')
            return cached
    a=audio(job/'audio.wav');duration=len(a)/16000;t0=time.monotonic()
    rec=sherpa_onnx.OfflineRecognizer.from_sense_voice(model=str(models/SENSE/'model.int8.onnx'),tokens=str(models/SENSE/'tokens.txt'),num_threads=threads,use_itn=True,language=language)
    p=subprocess.run(['ffmpeg','-v','info','-i',str(job/'audio.wav'),'-af','silencedetect=noise=-30dB:d=0.2','-f','null','-'],capture_output=True,text=True,check=True,timeout=min(resources.remaining(),resources.policy()['convert_seconds']))
    pauses=[float(x) for x in re.findall(r'silence_end: ([\d.]+)',p.stderr)]
    checkpoint=job/'asr.chunks.jsonl';sigfile=job/'asr.signature.json';chunks=[]
    same_signature=sigfile.exists() and read_json(sigfile).get('signature')==sig
    if checkpoint.exists() and same_signature:
        for line in checkpoint.read_text(encoding='utf-8').splitlines():
            expected=chunks[-1]['end'] if chunks else 0
            try:
                c=json.loads(line);_validate_chunk(c,expected,duration)
            except (ValueError,TypeError,KeyError):break
            chunks.append(c)
    # Replace the old checkpoint before making a changed signature visible.
    _write_text(checkpoint,''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in chunks))
    if not same_signature:write_json(sigfile,{'signature':sig})
    pos=chunks[-1]['end'] if chunks else 0;last=pos
    with checkpoint.open('a',encoding='utf-8') as f:
        while pos<duration:
            resources.remaining();resources.disk(job)
            choices=[t for t in pauses if pos+10<t<=min(pos+25,duration)]
            end=max(choices) if choices else min(pos+25,duration)
            stream=rec.create_stream();stream.accept_waveform(16000,a[int(pos*16000):int(end*16000)]);rec.decode_stream(stream)
            resources.remaining()
            r=stream.result;tokens=list(r.tokens);ts=[round(pos+t,3) for t in r.timestamps]
            if len(tokens)!=len(ts):raise ValueError('ASR token/time count mismatch')
            text=re.sub(r'<\|.*?\|>','',r.text).strip()
            if re.sub(r'\s','',''.join(tokens))!=re.sub(r'\s','',text):raise ValueError('ASR tokens differ from raw text')
            c={'start':pos,'end':end,'text':text,'tokens':tokens,'timestamps':ts};chunks.append(c)
            _validate_chunk(c,pos,duration)
            f.write(json.dumps(c,ensure_ascii=False)+'\n');f.flush();pos=end
            if pos-last>=120 or pos>=duration:
                print(f'ASR {pos/60:.1f}/{duration/60:.1f} min',flush=True);last=pos
    result={'signature':sig,'duration':duration,'elapsed_this_run':time.monotonic()-t0,'chunks':chunks}
    write_json(done,result);return result


def diarize(job, models, count=None, shift=.5):
    if count is not None and (not isinstance(count,int) or isinstance(count,bool) or count<1):raise ValueError('Speaker count must be a positive integer')
    job=Path(job);models=Path(models);duration=audio_duration(job/'audio.wav');t0=time.monotonic()
    if count==1:
        result={'segments':[{'start':0.,'end':duration,'speaker':0,'margin':1.}] if duration else [], 'report':{'count':1,'count_mode':'specified','accuracy_verified':False},'elapsed_this_run':0}
        write_json(job/'speakers.json',result);return result
    sig=signature(job,'segmentation-v1',{'shift':shift,'model':file_hash(models/SEG),'embedding':file_hash(models/EMBED)})
    region_file=job/'speaker-regions.json';feature_file=job/'speaker-features.npy'
    if region_file.exists() and read_json(region_file).get('signature')==sig and feature_file.exists():
        regions=read_json(region_file)['segments'];features=np.load(feature_file)
    else:
        a=audio(job/'audio.wav')
        config=sherpa_onnx.OfflineSpeakerDiarizationConfig(segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(models/SEG),window_shift_ratio=shift),num_threads=2),embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(models/EMBED),num_threads=2),clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1,threshold=.5),min_duration_on=.3,min_duration_off=.5)
        if not config.validate():raise ValueError('Diarization models missing')
        sd=sherpa_onnx.OfflineSpeakerDiarization(config);last=[-1]
        def cb(n,total):
            resources.remaining()
            step=int(n/total*10)
            if step>last[0]:print(f'SPEAKER regions {step*10}%',flush=True);last[0]=step
            return 0
        regions=[{'start':r.start,'end':r.end} for r in sd.process(a,callback=cb).sort_by_start_time()]
        resources.remaining()
        _validate_regions(regions,duration)
        if not regions:
            result={'segments':[],'report':{'count':0,'count_mode':'unavailable','error':'未找到可用说话人语音区间'},'elapsed_this_run':time.monotonic()-t0}
            write_json(job/'speakers.json',result);return result
        extractor=sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(models/EMBED),num_threads=2))
        features=[]
        for i,s in enumerate(regions):
            resources.remaining()
            mid=(s['start']+s['end'])/2;lo=max(s['start'],mid-3);hi=min(s['end'],mid+3)
            if hi-lo<.4:lo=max(0,mid-.2);hi=min(len(a)/16000,mid+.2)
            stream=extractor.create_stream();stream.accept_waveform(16000,a[int(lo*16000):int(hi*16000)]);stream.input_finished()
            if not extractor.is_ready(stream):raise ValueError('语音太短，无法提取声纹')
            features.append(extractor.compute(stream))
            if i%100==0:print(f'SPEAKER embeddings {i+1}/{len(regions)}',flush=True)
        features=np.asarray(features,dtype=np.float32)
        region_file.unlink(missing_ok=True)
        np.save(feature_file,features)
        write_json(region_file,{'signature':sig,'segments':regions})
    _validate_regions(regions,duration)
    labels,report=cluster_speakers(features,[s['end']-s['start'] for s in regions],count)
    # Stable chronological labels, not names inferred from transcript content.
    order={}
    for lab in labels:
        if int(lab) not in order:order[int(lab)]=len(order)
    margins=report.pop('margins')
    if len(regions)!=len(labels) or len(regions)!=len(margins):raise ValueError('Speaker region/label/margin count mismatch')
    segments=[{**s,'speaker':order[int(lab)],'margin':margin} for s,lab,margin in zip(regions,labels,margins)]
    result={'segments':segments,'report':report,'elapsed_this_run':time.monotonic()-t0}
    write_json(job/'speakers.json',result);return result


def combine(asr_result, speakers):
    _validate_asr(asr_result)
    regions=speakers['segments'];turns=[];original=[]
    _validate_regions(regions,labeled=True)
    # Sweep timestamps in time order, but emit tokens in their original order.
    # Adjacent ASR chunks can have overlapping timestamp tolerances.
    starts=sorted(range(len(regions)),key=lambda i:(regions[i]['start'],i))
    endpoints={}
    for i,s in enumerate(regions):
        for t in (s['start'],s['end']):endpoints[t]=min(endpoints.get(t,i),i)
    boundaries=sorted(endpoints);active={};ending=[];cursor=0;labels={}
    for t in sorted({t for c in asr_result['chunks'] for t in c['timestamps']}):
        while cursor<len(starts) and regions[starts[cursor]]['start']<=t:
            i=starts[cursor];active[i]=regions[i];heapq.heappush(ending,(regions[i]['end'],i));cursor+=1
        while ending and ending[0][0]<t:
            _,i=heapq.heappop(ending);active.pop(i,None)
        chosen=list(active.values())
        if not chosen and boundaries:
            j=bisect.bisect_left(boundaries,t)
            candidates=[boundaries[k] for k in (j-1,j) if 0<=k<len(boundaries)]
            boundary=min(candidates,key=lambda b:(abs(t-b),endpoints[b]))
            if abs(t-boundary)<=.6:chosen=[regions[endpoints[boundary]]]
        value='说话人未确定'
        if chosen:
            value='/'.join('说话人'+str(k+1) for k in sorted({s['speaker'] for s in chosen}))
            if any(s.get('margin',0)<.1 for s in chosen):value+='（待核对）'
        labels[t]=value
    last_timestamp=0.
    for chunk in asr_result['chunks']:
        for tok,t in zip(chunk['tokens'],chunk['timestamps']):
            original.append(tok);who=labels[t]
            t=max(last_timestamp,min(chunk['end'],max(chunk['start'],t)))
            last_timestamp=t
            end=max(t,min(t+.2,chunk['end']))
            if not turns or turns[-1]['speaker']!=who or t-turns[-1]['start']>25 or t-turns[-1]['end']>2:
                turns.append({'id':f't{len(turns)+1:05d}','start':t,'end':end,'speaker':who,'text':tok})
            else:turns[-1]['text']+=tok;turns[-1]['end']=max(turns[-1]['end'],end)
    if ''.join(t['text'] for t in turns)!=''.join(original):raise ValueError('角色合并改变了原始文字')
    turns=[t for t in turns if t['text'].strip()]
    for t in turns:t['text']=t['text'].strip()
    return turns
