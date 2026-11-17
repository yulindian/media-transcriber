"""Agent-neutral bootstrap, using only the Python standard library."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def install(args,root=None,execute=True):
    root=Path(root or Path(__file__).resolve().parents[1]).resolve()
    file=root/'config.json'
    cfg=json.loads((file if file.exists() else root/'config.example.json').read_text(encoding='utf-8-sig'))
    for key,override,default in [('runtime',args.runtime,'runtime'),('output',args.output,'outputs')]:
        path=Path(override or cfg.get(key) or default).expanduser()
        cfg[key]=str((path if path.is_absolute() else root/path).resolve())
    runtime=Path(cfg['runtime']);python=runtime/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    commands=[]
    if not python.is_file():commands.append(['uv','venv','--python','3.12',str(runtime/'.venv')])
    commands.extend([['uv','pip','install','--python',str(python),'-r',str(root/'requirements.txt')],
                     [str(python),'-X','utf8',str(root/'scripts/setup_models.py'),'--runtime',str(runtime)]])
    result={'status':'install_plan','config':cfg,'python':str(python),'commands':commands}
    if not execute:return result
    needed=['uv','ffmpeg','ffprobe']
    if cfg.get('anonymous_api',{}).get('enabled'):needed.append('curl.exe' if os.name=='nt' else 'curl')
    missing=[name for name in needed if not shutil.which(name)]
    if missing:raise ValueError('Missing dependencies: '+', '.join(missing)+'. Ask the host to provide these commands, then retry.')
    for command in commands:subprocess.run(command,cwd=root,check=True,timeout=1800,stdout=sys.stderr)
    # Publish configuration only after successful dependency/model setup.
    fd,name=tempfile.mkstemp(prefix='config-',suffix='.tmp',dir=root)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as stream:
            json.dump(cfg,stream,ensure_ascii=False,indent=2);stream.write('\n')
        Path(name).replace(file)
    finally:Path(name).unlink(missing_ok=True)
    diagnostic=subprocess.run([str(python),'-X','utf8',str(root/'scripts/transcribe.py'),'doctor'],cwd=root,check=True,timeout=120,capture_output=True,text=True,encoding='utf-8')
    if diagnostic.stderr:print(diagnostic.stderr,file=sys.stderr)
    health=json.loads(diagnostic.stdout)
    if health.get('status')!='ready':raise ValueError('Environment not ready: '+', '.join(health.get('missing',[]))+'. Run doctor for details.')
    return {**result,'status':'installed','health':health,'next':'Read SKILL.md and run transcribe.py with the returned Python path.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime');parser.add_argument('--output')
    parser.add_argument('--plan',action='store_true',help='Show paths and commands without writing files or installing')
    args=parser.parse_args()
    try:result=install(args,execute=not args.plan)
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        print(json.dumps({'status':'error','message':str(exc)},ensure_ascii=False));sys.exit(1)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
