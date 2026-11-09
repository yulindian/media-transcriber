"""Finite resource budgets shared with worker threads; no global config mutation."""
import math
import shutil
import time
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path

DEFAULTS = dict(max_download_bytes=2*1024**3, max_audio_seconds=7200,
                download_seconds=600, convert_seconds=600, metadata_seconds=120,
                task_seconds=14400, min_free_disk_bytes=512*1024**2,
                max_waveform_bytes=2*1024**3)
_ACTIVE = ContextVar('media_resource_budget', default=None)


class ResourceLimit(ValueError):
    pass


def limits(config=None):
    values={**DEFAULTS, **((config or {}).get('limits') or {})}
    for key,value in values.items():
        if key not in DEFAULTS or isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('Invalid resource limit: '+key)
    return values


def policy():
    active=_ACTIVE.get()
    return active[0] if active else DEFAULTS


@contextmanager
def scope(config):
    values=limits(config)
    end=time.monotonic()+values['task_seconds']
    parent=_ACTIVE.get()
    if parent:end=min(end,parent[1])
    token=_ACTIVE.set((values,end))
    try:yield
    finally:_ACTIVE.reset(token)


def deadline(seconds):
    end=time.monotonic()+seconds
    active=_ACTIVE.get()
    return min(end,active[1]) if active else end


def remaining(end=None):
    active=_ACTIVE.get()
    now=time.monotonic()
    ends=([end] if end is not None else [])+([active[1]] if active else [])
    value=min(ends)-now if ends else policy()['task_seconds']
    if value<=0:raise ResourceLimit('资源预算超时；保留缓存，可调整 limits 后续跑')
    return value


def disk(path, needed=0):
    path=Path(path).resolve()
    while not path.exists():path=path.parent
    if shutil.disk_usage(path).free < policy()['min_free_disk_bytes']+needed:
        raise ResourceLimit('可用磁盘不足；保留已有素材和缓存')


def audio(seconds, parallel=False):
    if not math.isfinite(seconds) or seconds<=0 or seconds>policy()['max_audio_seconds']:
        raise ResourceLimit('音轨时长超过 max_audio_seconds 或无效')
    # Conservative waveform conversion estimate; does not include model memory.
    if seconds*16000*4*(4 if parallel else 2)>policy()['max_waveform_bytes']:
        raise ResourceLimit('音轨波形估算超过 max_waveform_bytes')


def submit(pool, fn, *args, **kwargs):
    return pool.submit(copy_context().run,fn,*args,**kwargs)
