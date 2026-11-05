"""Download only missing official model bundles, reusing the tested local cache."""
import argparse
import tarfile
from pathlib import Path
import requests
from engine import SENSE, EMBED, SEG


def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',required=True);args=p.parse_args()
    base=Path(args.runtime)/'models';base.mkdir(parents=True,exist_ok=True)
    items=[(SENSE+'/model.int8.onnx','asr-models',SENSE+'.tar.bz2'),(SEG,'speaker-segmentation-models','sherpa-onnx-pyannote-segmentation-3-0.tar.bz2'),(EMBED,'speaker-recongition-models',EMBED)]
    for expected,tag,name in items:
        required=[base/expected]
        if name==SENSE+'.tar.bz2':required.append(base/SENSE/'tokens.txt')
        if all(p.is_file() and p.stat().st_size>0 for p in required):
            print('REUSE',expected,flush=True);continue
        url='https://github.com/k2-fsa/sherpa-onnx/releases/download/'+tag+'/'+name
        archive=base/name;tmp=archive.with_name(name+'.part')
        with requests.get(url,stream=True,timeout=(30,90)) as r:
            r.raise_for_status()
            with tmp.open('wb') as f:
                for chunk in r.iter_content(1024*1024):f.write(chunk)
        tmp.replace(archive)
        if name.endswith('.tar.bz2'):
            with tarfile.open(archive) as t:t.extractall(base,filter='data')
        if not (base/expected).is_file() or not (base/expected).stat().st_size:raise ValueError('Missing model after download: '+expected)
        print('READY',expected,flush=True)


if __name__=='__main__':main()
