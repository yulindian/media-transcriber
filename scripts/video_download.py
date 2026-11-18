"""Download a playable video without creating a transcription job."""
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

import documents as docs
import failures
import inputs
import resources
import sources


def inspect_media(path):
    try:
        result=subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file',
                               '-format_whitelist','mov,matroska,webm,avi,flv,mpegts,mpeg,ogg,mp3,wav,aac,flac,image2,png_pipe,jpeg_pipe',
                               '-show_entries','stream=codec_type:stream_disposition=attached_pic:format=format_name,duration:format_tags=major_brand',
                               '-of','json',str(path)],capture_output=True,encoding='utf-8',errors='replace',
                              timeout=min(60,resources.remaining()))
        if result.returncode==0:return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise ValueError('ffprobe 验证视频超时') from None
    raise ValueError('无法读取视频文件，未交付下载结果')


def probe_video(path):
    media=inspect_media(path)
    if not any(s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')
               for s in media.get('streams',[])):
        raise ValueError('当前入口没有视频画面（音轨或封面不能作为视频交付）')
    info=media.get('format',{});formats=set(info.get('format_name','').split(','))
    try:duration=float(info.get('duration'))
    except (ValueError,TypeError):duration=None
    if duration is not None and (not math.isfinite(duration) or duration<=0):
        raise ValueError('视频时长无效')
    if 'mp4' in formats:
        suffix='.mov' if info.get('tags',{}).get('major_brand','').strip()=='qt' else '.mp4'
    elif 'matroska' in formats:
        # The demuxer reports both names; preserve WebM only when named as such.
        suffix='.webm' if path.suffix.lower()=='.webm' else '.mkv'
    else:
        suffix=next((extension for name,extension in [('avi','.avi'),('flv','.flv'),('mpegts','.ts'),('mpeg','.mpg'),('ogg','.ogv')] if name in formats),None)
    if not suffix:raise ValueError('此视频封装格式暂不支持交付')
    return suffix,duration


def publish(source,output,stem,suffix):
    """Publish an intact copy; existing files and source are left untouched."""
    digest=docs.file_hash(source);n=1
    while True:
        path=output/(stem+('' if n==1 else f'-{n}')+suffix)
        if not path.exists() and not path.is_symlink():break
        if path.is_file() and not path.is_symlink() and docs.file_hash(path)==digest:return path
        n+=1
    resources.disk(output,source.stat().st_size)
    with tempfile.NamedTemporaryFile(dir=output,prefix='.video-',suffix='.part',delete=False) as target:
        temp=Path(target.name)
        try:
            with source.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    resources.remaining();target.write(chunk)
            target.close()
            if docs.file_hash(temp)!=digest:raise ValueError('复制后的视频校验失败')
            # Publish without overwriting a file created by another process.
            if os.name=='nt':temp.rename(path)
            else:path.hardlink_to(temp)
        finally:
            target.close();temp.unlink(missing_ok=True)
    return path


def download(value,cookies,config,output,lock):
    url=inputs.normalize(value)
    output=Path(output).resolve()
    staging=Path(config['runtime'])/'downloads'
    staging.mkdir(parents=True,exist_ok=True)
    with resources.scope(config), tempfile.TemporaryDirectory(prefix='video-',dir=staging) as temporary:
        job=Path(temporary)
        info=sources.metadata(url,cookies,config,video=True)
        def acquire(descriptor,directory):
            path=sources.acquire(descriptor,directory,cookies,video=True)
            if not descriptor.get('local_path') and not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError('下载器返回任务目录外的文件')
            suffix,duration=probe_video(path)
            return path,suffix,duration
        try:path,suffix,duration=acquire(info,job)
        except ValueError as error:
            import anonymous_sources as anonymous
            platform=sources.platform(url)
            if (cookies or failures.should_stop_refresh(error) or info.get('acquisition') not in ('yt-dlp','douyin_public_app')
                    or not anonymous.api_enabled(platform,config)):raise
            alt=job/'fallback';alt.mkdir()
            info=anonymous.api_metadata(url,platform,config)
            path,suffix,duration=acquire(info,alt)
        stem=docs.filename(info.get('title') or '视频')+'-'+inputs.key(url)[:10]
        with lock(output):result=publish(path,output,stem,suffix)
    return dict(status='video_ready',video_path=str(result),title=info.get('title'),duration_seconds=duration)
