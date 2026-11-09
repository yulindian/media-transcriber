"""Extract one input without changing signed queries; derive conservative identities."""
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def normalize(value):
    if not isinstance(value,str) or not value.strip():raise ValueError('输入为空，请提供分享链接或本地音视频路径')
    value=value.strip()
    if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"):value=value[1:-1]
    try:
        if Path(value).is_file():return str(Path(value).resolve())
    except (OSError,ValueError):pass
    matches=re.findall(r'https?://[^\s<>\[\]"\']+',value)
    direct=matches==[value]
    urls=[]
    for candidate in matches:
        # Only remove share/Markdown wrappers; query ordering and escapes stay intact.
        if not direct:
            candidate=candidate.rstrip('，。！？；：）】》')
            if candidate.endswith(')') and candidate.count(')')>candidate.count('('):candidate=candidate[:-1]
        if candidate not in urls:urls.append(candidate)
    if len(urls)!=1:raise ValueError('请提供单条链接；多条输入使用 batch 文件，不能猜测选择其中一条')
    parsed=urlsplit(urls[0])
    if not parsed.hostname or parsed.username or parsed.password:raise ValueError('链接无效或含账号凭据，请使用不带账号密码的 HTTP(S) 链接')
    return urls[0]


def identity(value):
    value=normalize(value)
    p=urlsplit(value);host=(p.hostname or '').lower();path=p.path.rstrip('/')
    if p.scheme not in ('http','https'):return 'file:'+os.path.normcase(str(Path(value).resolve()))
    query=parse_qs(p.query)
    youtube=host in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be','www.youtube-nocookie.com')
    if youtube and not query.get('list'):
        values=query.get('v',[])
        video=values[0] if path=='/watch' and len(values)==1 else None
        pattern=r'/([A-Za-z0-9_-]{6,})' if host=='youtu.be' else r'/(?:shorts|embed)/([A-Za-z0-9_-]{6,})'
        match=re.fullmatch(pattern,path)
        if not video and match:video=match[1]
        if video and re.fullmatch(r'[A-Za-z0-9_-]{6,}',video):return 'youtube:'+video
    if host in ('douyin.com','www.douyin.com','www.iesdouyin.com','iesdouyin.com','amemv.com','www.amemv.com'):
        match=re.fullmatch(r'/(?:share/)?video/(\d+)',path)
        values=query.get('modal_id',[])
        video=match[1] if match else None
        if video is None and path in ('','/recommend') and len(values)==1:video=values[0]
        if video and video.isdigit():return 'douyin:'+video
    if host in ('bilibili.com','www.bilibili.com','m.bilibili.com'):
        match=re.fullmatch(r'/video/(BV[A-Za-z0-9]+)',path)
        if match:return 'bilibili:'+match[1]+':part='+str((query.get('p') or ['1'])[0])
    if host in ('xiaoyuzhoufm.com','www.xiaoyuzhoufm.com'):
        match=re.fullmatch(r'/episode/([a-fA-F0-9]{24})',path)
        if match:return 'xiaoyuzhou:'+match[1].lower()
    if host in ('xiaohongshu.com','www.xiaohongshu.com'):
        match=re.fullmatch(r'/(?:explore|discovery/item)/([a-fA-F0-9]{16,})',path)
        if match:return 'xiaohongshu:'+match[1].lower()
    if host in ('weixin.qq.com','channels.weixin.qq.com'):
        match=re.fullmatch(r'/sph/([A-Za-z0-9_-]+)',path)
        if match:return 'channels:'+match[1]
    # Short links and unknown/signed media URLs never lose query identity.
    return 'url:'+value


def key(value, limit=None):
    return hashlib.sha256((identity(value)+'|limit='+str(limit)).encode()).hexdigest()[:20]


def job_path(value, runtime, limit=None, explicit=None):
    if explicit:return Path(explicit)
    normalized=normalize(value)
    # Keep existing exact-input jobs usable after adopting stable platform IDs.
    for original in dict.fromkeys((value.strip(),normalized)):
        old=hashlib.sha256((original+'|limit='+str(limit)).encode()).hexdigest()[:20]
        candidate=Path(runtime)/'jobs'/old
        if (candidate/'manifest.json').is_file():return candidate
    return Path(runtime)/'jobs'/key(normalized,limit)
