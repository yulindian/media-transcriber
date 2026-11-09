"""Anonymous public-media resolvers. No browser profiles or account credentials."""
import hashlib
import math
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
import failures
import resources
from public_http import get as public_get


PLATFORM_HOSTS = {
    '小红书': ('xiaohongshu.com', 'xhslink.cn', 'xhslink.com'),
    '小宇宙': ('xiaoyuzhoufm.com',),
    '抖音': ('douyin.com', 'iesdouyin.com', 'amemv.com'),
    '视频号': ('weixin.qq.com', 'channels.weixin.qq.com'),
    'B站': ('bilibili.com', 'b23.tv'),
    'YouTube': ('youtube.com', 'youtu.be'),
}
DEFAULT_API = 'https://api.bugpk.com/api/short_videos'


def _url(value):
    if not isinstance(value, str):
        raise ValueError('解析器没有返回有效媒体地址')
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
    except ValueError:
        raise ValueError('解析器返回的地址无效') from None
    return value


def _seconds(value, divisor=1):
    try:
        number = float(value) / divisor
        return number if not isinstance(value, bool) and math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def api_enabled(provider, config):
    options = (config or {}).get('anonymous_api') or {}
    return (isinstance(options, dict) and options.get('enabled') is True
            and isinstance(options.get('platforms'), list)
            and provider in options['platforms'])


def api_metadata(url, provider, config):
    """Try the explicitly configured services once each, with a fixed upper bound."""
    if not api_enabled(provider, config):
        raise ValueError('该平台的匿名解析服务未启用')
    options=config['anonymous_api']
    candidates=[options]
    fallbacks=options.get('fallbacks') or []
    if isinstance(fallbacks,list):
        candidates.extend({**options,**item} for item in fallbacks[:2]
                          if isinstance(item,dict) and provider in (item.get('platforms') or []))
    attempts=[]
    for candidate in candidates:
        record={'route':'anonymous_api','status':'failed'}
        endpoint=candidate.get('endpoint') or DEFAULT_API
        try:
            parsed=urlsplit(endpoint)
            if parsed.hostname and not parsed.username and not parsed.password:
                record['endpoint']=parsed.scheme+'://'+parsed.netloc+parsed.path
            result=_api_metadata_once(url,provider,{**config,'anonymous_api':candidate})
            record['status']='success';attempts.append(record)
            result['acquisition_attempts']=attempts
            return result
        except resources.ResourceLimit:raise
        except ValueError as exc:
            if failures.describe(exc)['error_code']=='rate_limited':raise
            attempts.append(record)
    raise ValueError('已配置的匿名解析入口均未取得可用视频；未取得音轨')


def _api_metadata_once(url, provider, config):
    """Send only an opted-in public share URL to the configured parse service."""
    if not api_enabled(provider, config):
        raise ValueError('该平台的匿名解析服务未启用')
    _url(url)
    host = urlsplit(url).hostname.lower()
    if not any(host == h or host.endswith('.' + h) for h in PLATFORM_HOSTS.get(provider, ())):
        raise ValueError('不向匿名服务发送未知平台或带凭据的地址')
    options = config['anonymous_api']
    endpoint = _url(options.get('endpoint') or DEFAULT_API)
    endpoint_host = urlsplit(endpoint).hostname
    if urlsplit(endpoint).scheme != 'https' and endpoint_host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('远程解析服务必须使用 HTTPS')
    try:
        timeout = min(max(float(options.get('timeout', 25)), 3), 45)
        response = public_get(endpoint, params={'url': url}, headers={
            'User-Agent': 'Mozilla/5.0 media-transcriber/1.0',
            'Accept': 'application/json',
            'Referer': 'https://api.bugpk.com/doc-short_videos.html' if endpoint == DEFAULT_API else endpoint,
            'X-Requested-With': 'XMLHttpRequest',
        }, timeout=(10, timeout), allow_curl_fallback=True,
           public_only=endpoint_host not in ('127.0.0.1','localhost','::1'), guard_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except resources.ResourceLimit:raise
    except (requests.RequestException, ValueError, TypeError) as exc:
        diagnostic=failures.describe(exc)
        if diagnostic['error_code']=='rate_limited':
            raise failures.WorkflowError('匿名解析服务限流，本次停止请求',diagnostic) from None
        raise ValueError('匿名解析服务请求失败或返回非 JSON，未取得音轨') from None
    if not isinstance(payload, dict) or payload.get('code') != 200:
        raise ValueError('匿名解析服务未取得公开视频，可能受内容权限或服务状态限制')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise ValueError('解析结果不是视频对象')
    schema=options.get('schema','bugpk')
    is_video=(data.get('type')=='video' if schema=='bugpk' else
              schema=='ynvan' and data.get('content_kind') in ('media','video')
              and isinstance(data.get('video_url'),str) and bool(data['video_url'])
              and not data.get('entries'))
    if not is_video:
        raise ValueError('解析结果不是单条视频，不能把图文或封面作为音轨')
    identity = _native_id(url)
    if identity:
        for field in ('id', 'video_id', 'aweme_id'):
            returned_id = data.get(field)
            if returned_id is not None and str(returned_id) and str(returned_id) != identity:
                raise ValueError('解析结果的作品 ID 与输入不一致')
    media_url = _url(data.get('url') or data.get('video_url'))
    page_url=data.get('page_url')
    if isinstance(page_url,str) and page_url:
        _url(page_url)
        if provider=='YouTube' and stable_id(page_url)!=stable_id(url):
            raise ValueError('解析结果不属于输入的 YouTube 视频')
        if provider=='视频号' and stable_id(page_url)!=stable_id(url):
            raise ValueError('解析结果不属于输入的视频号作品')
    transfer={'transfer':'curl'}
    if schema=='ynvan':
        signatures=data.get('sig') or {}
        signature=signatures.get(media_url) if isinstance(signatures,dict) else None
        if provider=='YouTube' and (not isinstance(signature,str) or not signature.strip()):
            raise ValueError('YouTube 解析结果缺少对应媒体的代理签名，未取得音轨')
        if isinstance(signature,str) and signature:
            media_url=endpoint.rsplit('/',1)[0]+'/proxy?'+urlencode({'url':media_url,'sig':signature,'download':'1'})
            transfer={'transfer':'curl','media_delivery':'resolver_proxy'}
    title = data.get('title') or data.get('desc')
    if not isinstance(title, str) or not title.strip():
        raise ValueError('解析结果缺少作品标题，无法核对来源')
    author = data.get('author') or {}
    return {'id': str(data.get('id') or stable_id(url)), 'title': title,
            'platform': provider, 'source_url': url, 'resolved_url': url,
            'media_url': media_url, 'duration': _seconds(data.get('duration')),
            'author': author.get('name', '') if isinstance(author, dict) else str(author),
            'acquisition': 'anonymous_api:' + endpoint_host,
            'resolver_service': endpoint_host, 'uses_user_credentials': False, **transfer}


def _native_id(url):
    """Return only an identity explicitly present in the source URL."""
    parsed=urlsplit(url)
    path = parsed.path.rstrip('/')
    if parsed.hostname in ('www.youtube.com','youtube.com','m.youtube.com'):
        value=parse_qs(parsed.query).get('v')
        if value and re.fullmatch(r'[A-Za-z0-9_-]{6,}',value[0]):return value[0]
        match=re.fullmatch(r'/(?:shorts|embed)/([A-Za-z0-9_-]{6,})',path)
        return match[1] if match else None
    if parsed.hostname=='youtu.be':
        candidate=path.lstrip('/')
        return candidate if re.fullmatch(r'[A-Za-z0-9_-]{6,}',candidate) else None
    host=(parsed.hostname or '').lower()
    if any(host==item or host.endswith('.'+item) for item in PLATFORM_HOSTS['B站']):
        match=re.search(r'/(BV[A-Za-z0-9]+)(?:/|$)',path)
        return match[1] if match else None
    if host in ('weixin.qq.com','channels.weixin.qq.com'):
        match=re.fullmatch(r'/sph/([A-Za-z0-9_-]+)',path)
        return match[1] if match else None
    if any(host==item or host.endswith('.'+item) for item in PLATFORM_HOSTS['抖音']):
        match=re.fullmatch(r'/(?:share/)?video/(\d+)',path)
        if match:return match[1]
        if path not in ('','/recommend'):return None
        values=parse_qs(parsed.query).get('modal_id',[])
        return values[0] if len(values)==1 and values[0].isdigit() else None
    return None


def stable_id(url):
    return _native_id(url) or hashlib.sha256(url.encode()).hexdigest()[:16]


def douyin_metadata(url, config=None):
    """Fetch a public item by ID, rejecting unrelated recommendations in the feed."""
    _url(url)
    original = url
    host = urlsplit(url).hostname.lower()
    if not any(host == h or host.endswith('.' + h) for h in PLATFORM_HOSTS['抖音']):
        raise ValueError('不是抖音作品链接')
    try:
        if _native_id(url) is None and (host=='v.douyin.com' or host=='amemv.com' or host.endswith('.amemv.com') or host=='iesdouyin.com' or host.endswith('.iesdouyin.com')):
            response = public_get(url, headers={'User-Agent': 'okhttp3'}, timeout=(10, 20), public_only=True)
            response.raise_for_status()
            url = _url(response.url)
            redirected_host = (urlsplit(url).hostname or '').lower()
            if not any(redirected_host == h or redirected_host.endswith('.' + h) for h in PLATFORM_HOSTS['抖音']):
                raise ValueError('抖音短链跳转到未知平台')
        identity=_native_id(url)
        if not identity:
            raise ValueError('需要单条抖音视频，不能把主页、图集或直播当成视频')
        response = public_get('https://aweme.snssdk.com/aweme/v1/feed/', params={
            'aweme_id': identity, 'aid': '1128', 'version_name': '13.2.0',
            'version_code': '130200', 'device_platform': 'android',
        }, headers={'User-Agent': 'okhttp3'}, timeout=(10, 25), public_only=True)
        response.raise_for_status()
        payload = response.json()
    except resources.ResourceLimit:raise
    except (requests.RequestException, TypeError) as exc:
        diagnostic=failures.describe(exc)
        if diagnostic['error_code']=='rate_limited':
            raise failures.WorkflowError('抖音公开入口限流，本次停止请求',diagnostic) from None
        raise ValueError('抖音公开接口请求失败，未取得音轨') from None
    except ValueError:
        raise ValueError('抖音作品链接或公开接口响应无效') from None
    if not isinstance(payload, dict) or payload.get('status_code') != 0:
        raise ValueError('抖音公开接口拒绝获取该作品')
    items = payload.get('aweme_list')
    if not isinstance(items, list):
        raise ValueError('抖音公开接口缺少作品列表')
    item = next((x for x in items if isinstance(x, dict) and str(x.get('aweme_id')) == identity), None)
    if not item or item.get('images'):
        raise ValueError('抖音接口未返回匹配的视频，不能使用推荐作品或图集')
    video = item.get('video') or {}
    if not isinstance(video, dict) or not isinstance(video.get('play_addr'), dict):
        raise ValueError('抖音作品缺少有效视频信息')
    urls = video['play_addr'].get('url_list')
    if not isinstance(urls, list) or not urls:
        raise ValueError('抖音作品没有可用音视频地址')
    return {'id': identity, 'platform': '抖音', 'source_url': original, 'resolved_url': url,
            'title': item.get('desc') or '抖音视频',
            'author': item.get('author', {}).get('nickname', '') if isinstance(item.get('author'), dict) else '',
            'duration': _seconds(video.get('duration'), 1000), 'media_url': _url(urls[0]),
            'acquisition': 'douyin_public_app', 'uses_user_credentials': False, 'transfer':'curl'}
