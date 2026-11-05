"""Reproducible short-audio integration benchmark; keeps all media/results outside Git."""
import argparse
import difflib
import json
import math
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import documents as docs
import engine
import transcribe


def write_wav(path, samples):
    pcm=(np.clip(samples,-1,1)*32767).astype(np.int16)
    with wave.open(str(path),'wb') as f:
        f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(pcm.tobytes())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--single',type=Path,required=True,help='Local mono PCM16 16kHz short single-speaker WAV')
    parser.add_argument('--dialogue',type=Path,required=True,help='Local mono PCM16 16kHz short two-speaker WAV')
    parser.add_argument('--runtime',type=Path,required=True,help='Existing runtime directory with models')
    parser.add_argument('--work',type=Path,help='New empty benchmark directory; default system temp')
    args=parser.parse_args()
    work=args.work or Path(tempfile.mkdtemp(prefix='media-transcriber-benchmark-'))
    work.mkdir(parents=True,exist_ok=True)
    if any(work.iterdir()):raise ValueError('Benchmark work directory must be empty to measure a cold start')
    samples={k:engine.audio(p) for k,p in [('single',args.single),('dialogue',args.dialogue)]}
    if any(len(s)/16000>120 for s in samples.values()):raise ValueError('Use short WAV samples <=120 seconds, not a full episode')
    rng=np.random.default_rng(20261103);noise=rng.normal(0,.01,len(samples['dialogue'])).astype(np.float32)
    noisy=work/'noisy.wav';silence=work/'silence.wav'
    write_wav(noisy,samples['dialogue']+noise);write_wav(silence,np.zeros(48000,dtype=np.float32))
    cases=[('single',args.single),('dialogue',args.dialogue),('dialogue_noise',noisy),('silence',silence)]
    results={};config={'runtime':str(args.runtime.resolve()),'output':str(work/'output'),'threads':4,'segmentation_shift':.5}
    for name,path in cases:
        job=work/name
        command=argparse.Namespace(input=str(path.resolve()),job=str(job),output=None,limit_seconds=None,cookies=None,source_url=None,title=None,speakers='auto',speaker_evidence='')
        started=time.perf_counter()
        try:
            result=transcribe.run(command,config)
            elapsed=time.perf_counter()-started
            turns=docs.read_json(job/'turns.raw.json');text=''.join(t['text'] for t in turns)
            warm=[];exact=[]
            for _ in range(3):
                t=time.perf_counter();transcribe.run(command,config);warm.append(time.perf_counter()-t)
                exact.append(turns==docs.read_json(job/'turns.raw.json'))
            results[name]={'status':result['status'],'audio_seconds':result['audio_seconds'],'cold_seconds':elapsed,'warm_seconds':warm,'warm_exact_match':all(exact),'speaker_count':result['speaker_report'].get('count'),'speaker_count_mode':result['speaker_report'].get('count_mode'),'turns':len(turns),'characters':len(text),'warnings':result.get('warnings',[])}
            print(name,json.dumps(results[name],ensure_ascii=False),flush=True)
        except Exception as exc:
            results[name]={'status':'rejected','elapsed_seconds':time.perf_counter()-started,'reason':str(exc)}
            print(name,json.dumps(results[name],ensure_ascii=False),flush=True)
    clean_path=work/'dialogue'/'turns.raw.json';noisy_path=work/'dialogue_noise'/'turns.raw.json'
    if clean_path.exists() and noisy_path.exists():
        clean=''.join(t['text'] for t in docs.read_json(clean_path));noisy_text=''.join(t['text'] for t in docs.read_json(noisy_path))
        results['noise_comparison']={'snr_db':10*math.log10(float(np.mean(samples['dialogue']**2))/float(np.mean(noise**2))),'sequence_similarity_to_clean_asr':difflib.SequenceMatcher(None,clean,noisy_text).ratio(),'note':'Agreement with clean machine ASR only; not human-reference accuracy or WER/CER.'}
    report={'cases':results,'runtime':'CPU; model decoding enabled on short samples','scope':'full input files, not limited excerpts; each starts unreviewed; no accuracy ground truth','work':str(work)}
    docs.write_json(work/'benchmark-results.json',report)
    print('RESULTS',work/'benchmark-results.json',flush=True)


if __name__=='__main__':main()
