"""Public media acquisition. Authentication failures are reported, not bypassed."""
import hashlib
import json
import re
import shutil
import subprocess
import wave
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies

import requests
import resources
import failures
from public_http import check_public_url, curl_redirects, get as public_get

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36'


def redact(value):
    value = re.sub(r'(?i)(https?://)[^/\s@]+@', r'\1[redacted]@', str(value))
    value = re.sub(r'(?i)([?&][^=\s&]+)=[^&\s]+', r'\1=[redacted]', str(value))
    return re.sub(r'(?i)(cookie|authorization):[^\n]+', r'\1: [redacted]', value)[:1500]


def http_url(value):
    p = urlsplit(value)
    if p.scheme not in ('http', 'https') or not p.hostname:
        raise ValueError('Expected HTTP(S) URL')
    return value


def public_page(url):
    try:
        response = public_get(url, headers={'User-Agent': UA}, timeout=30, allow_curl_fallback=True, public_only=True)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise ValueError(f'公开页面获取失败：{redact(exc)}') from None


def normalize_xhs(url, _depth=0):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or p.hostname not in ('www.xiaohongshu.com', 'xiaohongshu.com'):
        return url
    if _depth >= 4:
        raise ValueError('小红书跳转层数过多')
    redirect = parse_qs(p.query).get('redirectPath')
    if redirect:
        target = urljoin(url, redirect[0])
        if urlsplit(target).hostname not in ('www.xiaohongshu.com', 'xiaohongshu.com'):
            raise ValueError('小红书跳转目标不是受支持的小红书页面')
        return normalize_xhs(target, _depth + 1)
    m = re.search(r'/(?:explore|discovery/item)/([a-f0-9]+)', p.path)
    if not m or p.hostname not in ('www.xiaohongshu.com', 'xiaohongshu.com'):
        return url
    # Keep signed query bytes (including ordering, duplicates, and escaping).
    return urlunsplit(('https', 'www.xiaohongshu.com', '/explore/' + m[1], p.query, p.fragment))


def platform(url):
    h = (urlsplit(url).hostname or '').lower()
    for hosts, name in [(('xiaohongshu.com','xhslink.com','xhslink.cn'),'小红书'),(('xiaoyuzhoufm.com',),'小宇宙'),(('douyin.com','iesdouyin.com','amemv.com'),'抖音'),(('channels.weixin.qq.com','weixin.qq.com'),'视频号'),(('bilibili.com','b23.tv'),'B站'),(('youtube.com','youtu.be'),'YouTube')]:
        if any(h == x or h.endswith('.'+x) for x in hosts):
            return name
    return '其他平台'


class QuietLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def metadata(url, cookies=None, config=None):
    """Try bounded anonymous routes, preserving explicitly configured resolvers."""
    import anonymous_sources as anonymous
    config = config or {}
    provider = platform(url)
    if Path(url).is_file() or config.get('resolvers', {}).get(urlsplit(url).hostname):
        return _metadata_native(url, cookies, config)
    attempts = []
    if provider == '视频号' and not cookies and anonymous.api_enabled(provider, config):
        result = anonymous.api_metadata(url, provider, config)
        result.setdefault('acquisition_attempts',[{'route':'anonymous_api','status':'success'}])
        return result
    if provider == '抖音' and not cookies:
        attempts.append({'route':'douyin_public_app','status':'failed'})
        try:
            result = anonymous.douyin_metadata(url, config)
            attempts[-1]['status']='success';result['acquisition_attempts'] = attempts
            return result
        except resources.ResourceLimit:raise
        except ValueError as exc:
            if failures.describe(exc)['error_code']=='rate_limited':raise
            pass
    attempts.append({'route':'native','status':'failed'})
    try:
        result = _metadata_native(url, cookies, config)
        attempts[-1]['status']='success';result['acquisition_attempts'] = attempts
        result['uses_user_credentials'] = bool(cookies)
        return result
    except resources.ResourceLimit:raise
    except ValueError as native_error:
        if failures.describe(native_error)['error_code'] in ('source_unavailable','rate_limited'):raise
        if cookies or not anonymous.api_enabled(provider, config):
            raise
        attempts.append({'route':'anonymous_api','status':'failed'})
        try:
            result = anonymous.api_metadata(url, provider, config)
            attempts[-1]['status']='success';result['acquisition_attempts'] = attempts[:-1]+result.get('acquisition_attempts',[attempts[-1]])
            return result
        except resources.ResourceLimit:raise
        except ValueError as api_error:
            raise ValueError(f'{provider}公开入口不可用：{redact(native_error)}；{redact(api_error)}。未取得音轨，不要求用户登录，可提供媒体文件继续。') from None


def _metadata_native(url, cookies=None, config=None):
    """No media download. Return a serializable source descriptor."""
    config = config or {}
    if Path(url).is_file():
        p = Path(url).resolve()
        return {'id':'local-'+hashlib.sha256(str(p).encode()).hexdigest()[:16], 'title':p.stem, 'source_url':str(p), 'platform':'本地文件','local_path':str(p),'duration':None}
    http_url(url)
    original = url
    provider = platform(url)
    if provider == '小宇宙':
        r = public_page(url)
        m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            raise ValueError('小宇宙页面没有公开节目数据，可能需登录或页面结构变化')
        e = json.loads(m[1])['props']['pageProps']['episode']
        media = e.get('media', {}).get('source', {})
        if e.get('isPrivateMedia') or (media.get('mode') and media['mode'] != 'PUBLIC'):
            raise ValueError('该节目音频不是公开资源，需要合法访问入口')
        media_url = e.get('enclosure',{}).get('url') or media.get('url')
        if not media_url:
            raise ValueError('节目页未提供可下载的公开音频')
        return {'id':e['eid'],'platform':provider,'title':e['title'],'description':e.get('description',''),'author':e.get('podcast',{}).get('author',''),'podcast':e.get('podcast',{}).get('title',''),'source_url':original,'resolved_url':r.url,'media_url':http_url(media_url),'duration':e.get('duration'),'published_at':e.get('pubDate',''),'acquisition':'public_episode_page'}
    if provider == '小红书':
        if (urlsplit(url).hostname or '') in ('xhslink.cn','xhslink.com'):
            r=public_page(url)
            url=r.url
            if urlsplit(url).hostname not in ('www.xiaohongshu.com', 'xiaohongshu.com'):
                raise ValueError('小红书短链没有跳转到受支持的小红书页面')
        url=normalize_xhs(url)
    # Optional explicitly configured resolver; arguments are a list, never a shell string.
    host=urlsplit(url).hostname
    adapter=config.get('resolvers',{}).get(host)
    if adapter:
        if not isinstance(adapter,list) or not all(isinstance(x,str) for x in adapter):
            raise ValueError('Resolver must be a JSON array of command arguments')
        try:
            result=subprocess.run([x.replace('{url}',url) for x in adapter],capture_output=True,text=True,encoding='utf-8',timeout=min(90,resources.remaining(),resources.policy()['metadata_seconds']),check=True)
            d=json.loads(result.stdout)
            if not isinstance(d, dict) or not isinstance(d.get('media_url'), str):
                raise ValueError('Resolver must return an object with media_url')
            http_url(d['media_url'])
        except resources.ResourceLimit:raise
        except subprocess.TimeoutExpired:
            raise ValueError('配置的解析器超时，本次未取得音轨') from None
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            # Commands, stdout and stderr can include credentials; do not echo them.
            raise ValueError('配置的解析器失败或返回无效音视频地址，本次未取得音轨') from None
        return {**d,'source_url':original,'platform':provider,'id':str(d.get('id') or hashlib.sha256(original.encode()).hexdigest()[:16]),'title':d.get('title','音视频转录'),'acquisition':'configured_resolver'}
    check_public_url(url)
    from yt_dlp import YoutubeDL
    opts={'quiet':True,'no_warnings':True,'noplaylist':True,'socket_timeout':15,'retries':0,'extractor_retries':0,'logger':QuietLogger(),'format':'bestaudio/best','usenetrc':False}
    if provider=='抖音':opts['http_headers']={'Referer':'https://www.douyin.com/','User-Agent':UA}
    if shutil.which('node'):opts['js_runtimes']={'node':{'path':shutil.which('node')}}
    if cookies: opts['cookiefile']=str(cookies)
    try:
        with YoutubeDL(opts) as ydl:
            info=ydl.extract_info(url,download=False)
    except Exception as exc:
        raise ValueError(f'{provider}解析失败：{redact(exc)}。可用已授权的 cookies 文件、公开音视频直链或本地文件继续；本次未取得音轨。') from None
    if not info or info.get('_type') in ('playlist','multi_video'):
        raise ValueError('需要单条节目/视频链接，不能把合集预览作为单条全文')
    if not info.get('url') and not any(f.get('url') for f in info.get('formats', []) if isinstance(f, dict)):
        raise ValueError(f'{provider}未提供可播放音视频，可能需要有效授权；本次未取得音轨。')
    descriptor={'id':str(info.get('id') or hashlib.sha256(url.encode()).hexdigest()[:16]),'platform':provider,'source_url':original,'resolved_url':url,'title':info.get('title') or '音视频转录','description':info.get('description',''),'author':info.get('uploader',''),'duration':info.get('duration'),'published_at':info.get('upload_date',''),'acquisition':'yt-dlp'}
    if not cookies and info.get('protocol') in ('http','https') and isinstance(info.get('url'),str):
        # Reuse the selected HTTP audio URL instead of extracting the page twice.
        # HLS/DASH manifests still use yt-dlp's downloader.
        descriptor['media_url']=http_url(info['url'])
        headers=info.get('http_headers') or {}
        descriptor['media_headers']={k:v for k,v in headers.items()
                                     if k.lower() in ('referer','user-agent','origin','accept') and isinstance(v,str)}
    return descriptor


def _curl_download(url, target, public_only=True):
    budget=resources.policy();seconds=min(300,budget['download_seconds'],resources.remaining())
    resources.disk(target,budget['max_download_bytes'])
    command = ['curl', '--disable', '--fail', '--silent', '--show-error',
               '--proto', '=http,https', '--proto-redir', '=http,https', '--max-redirs', '5',
               '--connect-timeout', '15', '--max-time', str(seconds), '--max-filesize',str(int(budget['max_download_bytes'])), '--user-agent', UA,
               '--header', 'Accept-Encoding: identity', '--output', str(target)]
    proxies = getproxies()
    scheme = urlsplit(url).scheme
    proxy = proxies.get(scheme) or proxies.get('https') or proxies.get('http')
    if proxy:
        command += ['--proxy', proxy]
    try:
        result, output, _ = curl_redirects(command, http_url(url),
            '%{http_code}\n%{content_type}\n%{size_download}', seconds, public_only=public_only)
    except (OSError, subprocess.SubprocessError):
        # A subprocess exception embeds the signed command; never forward it.
        raise ValueError('curl 媒体传输失败或超时') from None
    if result.returncode:
        raise ValueError('curl 媒体传输失败')
    try:
        status, content_type, reported = output.strip().split('\n')
        content_type = content_type.split(';')[0].strip().lower()
        expected = int(reported)
    except (ValueError, AttributeError):
        raise ValueError('curl 媒体传输状态无效') from None
    if status != '200':
        raise ValueError('curl 未取得完整音视频媒体')
    if (content_type.startswith(('text/', 'image/')) or content_type.endswith(('+json', '+xml'))
            or content_type in ('application/json', 'application/xhtml+xml', 'application/xml')):
        raise ValueError('服务器返回的不是音视频媒体')
    size = target.stat().st_size
    if not size or size != expected or size>budget['max_download_bytes']:
        raise ValueError('音视频媒体下载长度不匹配或为空')
    with target.open('rb') as stream:
        prefix = stream.read(512).lstrip(b'\xef\xbb\xbf \r\n\t').lower()
    if prefix.startswith((b'<', b'{', b'[')):
        raise ValueError('服务器返回非音视频媒体')
    return size, file_hash(target)


def acquire(info, job, cookies=None):
    job=Path(job)
    budget=resources.policy();end=resources.deadline(budget['download_seconds'])
    resources.disk(job)
    if info.get('local_path'):
        if Path(info['local_path']).stat().st_size>budget['max_download_bytes']:raise resources.ResourceLimit('本地媒体超过 max_download_bytes')
        return Path(info['local_path'])
    if info.get('media_url'):
        public_only=info.get('acquisition')!='configured_resolver'
        suffix=Path(urlsplit(info['media_url']).path).suffix
        if suffix not in ('.m4a','.mp3','.wav','.mp4','.webm','.ogg','.flac'): suffix='.media'
        out=job/('source'+suffix)
        manifest=out.with_suffix(out.suffix+'.json')
        url_hash=hashlib.sha256(info['media_url'].encode()).hexdigest()
        if out.is_file() and manifest.is_file():
            try:
                saved=json.loads(manifest.read_text(encoding='utf-8'))
                if (saved.get('url_hash') == url_hash and saved.get('size') == out.stat().st_size
                        and saved.get('size', 0) > 0 and saved.get('sha256') == file_hash(out)):
                    if out.stat().st_size>budget['max_download_bytes']:raise resources.ResourceLimit('缓存媒体超过 max_download_bytes')
                    return out
            except resources.ResourceLimit:raise
            except (OSError, ValueError, AttributeError):
                pass
        tmp=out.with_suffix(out.suffix+'.part')
        manifest_tmp=manifest.with_suffix(manifest.suffix+'.part')
        try:
            if info.get('transfer') == 'curl':
                size, media_digest = _curl_download(info['media_url'], tmp, public_only)
            else:
                extra=info.get('media_headers') or {}
                safe_headers={k:v for k,v in extra.items() if k.lower() in ('referer','user-agent','origin','accept') and isinstance(v,str)}
                with public_get(http_url(info['media_url']),headers={'User-Agent':UA,**safe_headers,'Accept-Encoding':'identity'},stream=True,timeout=(min(25,resources.remaining(end)),min(90,resources.remaining(end))),public_only=public_only,guard_redirects=True) as r:
                    r.raise_for_status()
                    if r.status_code==206:raise ValueError('服务器只返回部分音视频媒体')
                    content_type=r.headers.get('Content-Type','').split(';')[0].strip().lower()
                    if content_type.startswith(('text/','image/')) or content_type in ('application/json','application/xhtml+xml','application/xml'):
                        raise ValueError('服务器返回的不是音视频媒体')
                    size=0
                    length=r.headers.get('Content-Length')
                    if length is not None and int(length)>budget['max_download_bytes']:raise resources.ResourceLimit('媒体超过 max_download_bytes')
                    head=b''
                    digest=hashlib.sha256()
                    with tmp.open('wb') as f:
                        for chunk in r.iter_content(1024*1024):
                            resources.remaining(end)
                            if not chunk: continue
                            if len(head)<512: head=(head+chunk)[:512]
                            size+=len(chunk)
                            if size>budget['max_download_bytes']:raise resources.ResourceLimit('媒体超过 max_download_bytes')
                            resources.disk(job,len(chunk))
                            digest.update(chunk)
                            f.write(chunk)
                    prefix=head.lstrip(b'\xef\xbb\xbf \r\n\t').lower()
                    if not size or prefix.startswith((b'<',b'{',b'[')):
                        raise ValueError('服务器返回空文件或非音视频媒体')
                    length=r.headers.get('Content-Length')
                    if length is not None and not r.headers.get('Content-Encoding') and size != int(length):
                        raise ValueError('音视频媒体下载长度不匹配')
                media_digest = digest.hexdigest()
            manifest_tmp.write_text(json.dumps({'url_hash':url_hash,'size':size,'sha256':media_digest}),encoding='utf-8')
            # Invalidate old metadata before publishing: a failed replacement cannot validate an old file.
            manifest.unlink(missing_ok=True)
            tmp.replace(out)
            manifest_tmp.replace(manifest)
        except resources.ResourceLimit:raise
        except (requests.RequestException, OSError, ValueError) as exc:
            raise ValueError(f'公开音视频下载失败：{redact(exc)}') from None
        finally:
            tmp.unlink(missing_ok=True)
            manifest_tmp.unlink(missing_ok=True)
        return out
    from yt_dlp import YoutubeDL
    blocked=[None]
    def progress(state):
        try:
            resources.remaining(end);resources.disk(job)
            if max(state.get('downloaded_bytes') or 0,state.get('total_bytes') or 0)>budget['max_download_bytes']:
                raise resources.ResourceLimit('媒体超过 max_download_bytes')
        except resources.ResourceLimit as exc:blocked[0]=exc;raise
    opts={'quiet':True,'no_warnings':True,'noplaylist':True,'socket_timeout':min(25,resources.remaining(end)),'retries':1,'logger':QuietLogger(),'format':'bestaudio/best','outtmpl':str(job/'source.%(ext)s'),'max_filesize':int(budget['max_download_bytes']),'progress_hooks':[progress]}
    if info.get('platform')=='抖音':opts['http_headers']={'Referer':'https://www.douyin.com/','User-Agent':UA}
    if cookies: opts['cookiefile']=str(cookies)
    try:
        with YoutubeDL(opts) as ydl:
            result=ydl.extract_info(info['resolved_url'],download=True)
            if not isinstance(result, dict):
                raise ValueError('下载器未取得单条音视频')
            progress({'total_bytes':result.get('filesize') or result.get('filesize_approx') or 0})
            paths=[Path(x['filepath']) for x in result.get('requested_downloads',[]) if x.get('filepath')]
            paths.append(Path(ydl.prepare_filename(result)))
    except resources.ResourceLimit:raise
    except Exception as exc:
        if blocked[0] is not None:raise blocked[0]
        raise ValueError(f'音视频下载失败：{redact(exc)}。本次未取得音轨。') from None
    for p in paths:
        if p.exists() and p.stat().st_size>0:
            if not p.resolve().is_relative_to(job.resolve()):
                raise ValueError('下载器返回任务目录外的文件，拒绝作为本次获取结果')
            progress({'downloaded_bytes':p.stat().st_size});return p
    raise ValueError('下载器没有产生有效音视频文件')


def file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def convert(media, target, limit=None):
    target=Path(target)
    tmp=target.with_name(target.stem+'.converting.wav')
    cmd=['ffmpeg','-v','error','-y','-i',str(media),'-vn','-ar','16000','-ac','1']
    budget=resources.policy()
    if limit and limit>budget['max_audio_seconds']:raise resources.ResourceLimit('试跑时长超过 max_audio_seconds')
    cap=limit if limit else budget['max_audio_seconds']
    # One extra second distinguishes oversized inputs from a complete capped file.
    resources.disk(target,(cap+(0 if limit else 1))*32000)
    cmd+=['-t',str(cap if limit else cap+1)]
    try:
        subprocess.run(cmd+[str(tmp)],check=True,capture_output=True,timeout=min(resources.remaining(),budget['convert_seconds']))
        with wave.open(str(tmp),'rb') as f:
            if f.getframerate()!=16000 or f.getnchannels()!=1 or f.getsampwidth()!=2 or f.getnframes()==0:
                raise ValueError('FFmpeg output is not valid PCM16 mono 16kHz audio')
            actual=f.getnframes()/16000
        resources.audio(actual,parallel=True)
        tmp.replace(target)
    except subprocess.TimeoutExpired:raise resources.ResourceLimit('FFmpeg 转换超过 convert_seconds') from None
    finally:tmp.unlink(missing_ok=True)
