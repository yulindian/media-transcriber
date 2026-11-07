"""Safe public provenance, separate from full local acquisition descriptors."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def source_url(value):
    try:
        parsed=urlsplit(str(value))
        if parsed.scheme not in ('http','https') or not parsed.hostname:return '[local source]'
        host=parsed.hostname.lower()
        netloc=('['+host+']' if ':' in host else host)+((':'+str(parsed.port)) if parsed.port else '')
        # Only public identity parameters on recognized platforms survive.
        allowed=set()
        if host in ('youtube.com','www.youtube.com','m.youtube.com'):allowed={'v'}
        elif host in ('bilibili.com','www.bilibili.com','m.bilibili.com'):allowed={'bvid','aid','p'}
        elif host in ('douyin.com','www.douyin.com'):allowed={'modal_id'}
        query=urlencode([(k,v) for k,v in parse_qsl(parsed.query,keep_blank_values=True)
                         if k in allowed and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',v)])
        return urlunsplit((parsed.scheme,netloc,parsed.path,query,''))
    except (TypeError,ValueError):return '[unavailable source]'


def audit(value):
    """Keep speech verbatim; strip private metadata from the shareable audit copy."""
    if isinstance(value,list):return [audit(v) for v in value]
    if not isinstance(value,dict):return value
    safe={}
    for key,item in value.items():
        if key=='clip_path':safe[key]='review-clips/'+str(item).replace('\\','/').rsplit('/',1)[-1]
        elif key in ('reason','evidence','note') and isinstance(item,str):
            safe[key]=re.sub(r'https?://[^\s<>"\']+',lambda m:source_url(m.group()),item)
        else:safe[key]=audit(item)
    return safe
