"""Isolated public HTTP requests with network proxies but no account state."""
import ipaddress
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import getproxies, proxy_bypass

import requests
import resources

MAX_NON_STREAM_BYTES = 8 * 1024 * 1024
_CURL_HEADERS = frozenset(('user-agent', 'accept', 'accept-language', 'referer', 'x-requested-with'))


def _check_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password):
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise ValueError('Public HTTP requires an HTTP(S) URL without account credentials') from None


def check_public_url(url):
    parsed = _check_url(url)
    host = parsed.hostname.rstrip('.').lower()
    if host == 'localhost' or host.endswith('.localhost'):
        raise ValueError('公开下载禁止访问本地或私有网络地址')
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(entry[4][0]) for entry in addresses]
    except (OSError, ValueError):
        raise ValueError('无法核验公开下载地址') from None
    if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
        raise ValueError('公开下载禁止访问本地或私有网络地址')
    return parsed


def curl_redirects(command, url, output_format, timeout, *, public_only, follow=True):
    """Fetch one hop per curl process so each target is checked before use."""
    deadline = time.monotonic() + timeout
    for hop in range(6):
        if public_only or hop:
            check_public_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('公开下载超时')
        result = subprocess.run(
            [*command, '--write-out', output_format + '\n%{redirect_url}', url],
            shell=False, capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=remaining,
        )
        fields = result.stdout.split('\n', 3)
        if (result.returncode or not follow or len(fields) < 4
                or fields[0] not in ('301','302','303','307','308') or not fields[3].strip()):
            return result, '\n'.join(fields[:3]), url
        url = urljoin(url, fields[3].strip())
    raise ValueError('公开下载跳转次数超限')


def _curl_get(url, kwargs, proxies):
    """One anonymous, bounded alternative transport; no stderr or command escapes."""
    prepared = requests.Request('GET', url, params=kwargs.get('params')).prepare().url
    parsed = _check_url(prepared)
    headers = {'User-Agent': 'Mozilla/5.0 media-transcriber/1.0', 'Accept': '*/*'}
    for name, value in (kwargs.get('headers') or {}).items():
        if str(name).lower() in _CURL_HEADERS:
            if not isinstance(value, str) or '\n' in value or '\r' in value:
                raise ValueError('Public HTTP fallback header is invalid') from None
            for existing in list(headers):
                if existing.lower() == str(name).lower():
                    del headers[existing]
            headers[str(name)] = value
    command = ['curl', '--disable', '--no-netrc', '--no-netrc-optional',
               '--silent', '--show-error', '--proto', '=http,https',
               '--proto-redir', '=http,https', '--max-redirs', '5',
               '--connect-timeout', '15', '--max-time', '60', '--retry', '0',
               '--max-filesize', str(MAX_NON_STREAM_BYTES),
               '--header', 'Accept-Encoding: identity']
    public_only = kwargs.get('public_only', False)
    if not public_only and not kwargs.get('guard_redirects') and kwargs.get('allow_redirects', True):
        command.append('--location')
    for name, value in headers.items():
        command += ['--header', name + ': ' + value]
    # An explicit empty proxy prevents curl from re-reading proxy environment
    # variables when the system bypass policy selected a direct connection.
    proxy = proxies.get(parsed.scheme) or proxies.get('all') or ''
    command += ['--proxy', proxy]
    with tempfile.TemporaryDirectory(prefix='y0-public-http-') as directory:
        body = Path(directory) / 'body'
        command += ['--output', str(body)]
        output_format = '%{http_code}\n%{content_type}\n%{url_effective}'
        try:
            result, output, _ = curl_redirects(
                command, prepared, output_format, min(65,resources.remaining(),resources.policy()['metadata_seconds']), public_only=public_only,
                follow=kwargs.get('allow_redirects', True))
        except (OSError, subprocess.SubprocessError):
            raise ValueError('Public HTTP fallback transfer failed or timed out') from None
        if result.returncode:
            raise ValueError('Public HTTP fallback transfer failed') from None
        try:
            status, content_type, effective = output.strip().split('\n', 2)
            status = int(status)
            if not 100 <= status <= 599:
                raise ValueError()
            _check_url(effective)
            if body.stat().st_size > MAX_NON_STREAM_BYTES:
                raise ValueError()
            content = body.read_bytes()
        except (OSError, TypeError, ValueError):
            raise ValueError('Public HTTP fallback returned an invalid or oversized response') from None
    response = requests.Response()
    response.status_code = status
    response.url = effective
    response.headers['Content-Type'] = content_type
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    response._content = content
    response._content_consumed = True
    return response


def _bypass(host):
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        pass
    return proxy_bypass(host)


def get(url, *, allow_curl_fallback=False, public_only=False, guard_redirects=False, **kwargs):
    """Make one GET using a fresh session; a streamed response owns its session."""
    original_url = url
    parsed = check_public_url(url) if public_only else _check_url(url)
    headers = kwargs.get('headers') or {}
    if (kwargs.get('auth') or kwargs.get('cookies')
            or any(str(key).lower() in ('authorization', 'cookie') for key in headers)):
        raise ValueError('Public HTTP does not accept account credentials')

    # trust_env=False disables requests' implicit .netrc lookup, including redirects.
    # Inherit only the transport proxies so existing network connectivity is preserved.
    proxies = kwargs.pop('proxies', None)
    proxies = getproxies() if proxies is None else proxies
    inherited = {} if _bypass(parsed.hostname.lower()) else {
        key: value for key, value in proxies.items() if key in ('http', 'https', 'all') and value
    }
    kwargs.setdefault('timeout', (10, 30))
    end=resources.deadline(resources.policy()['metadata_seconds'])
    wire={**kwargs,'stream':True}
    timeout=kwargs['timeout']
    pair=timeout if isinstance(timeout,tuple) else (timeout,timeout)
    wire['timeout']=tuple(min(float(v),resources.remaining(end)) for v in pair)
    session = requests.Session()
    session.trust_env = False
    session.proxies = inherited
    try:
        if public_only or guard_redirects:
            options = dict(wire)
            follow = options.pop('allow_redirects', True)
            for hop in range(6):
                options['timeout']=tuple(min(v,resources.remaining(end)) for v in wire['timeout'])
                result = session.get(url, allow_redirects=False, **options)
                if not follow or not result.is_redirect:
                    break
                target = urljoin(url, result.headers['Location'])
                result.close()
                if hop == 5:
                    raise ValueError('公开下载跳转次数超限')
                check_public_url(target)
                url = target
                options.pop('params', None)
        else:
            result = session.get(url, **wire)
        _check_url(result.url)
        if kwargs.get('stream'):
            close_response = result.close
            closed = False

            def close():
                nonlocal closed
                if not closed:
                    closed = True
                    try:
                        close_response()
                    finally:
                        session.close()

            result.close = close
            return result
        # Enforce the limit during transfer, before buffering an unbounded body.
        if result.headers.get('Content-Length') and int(result.headers['Content-Length'])>MAX_NON_STREAM_BYTES:
            result.close();raise ValueError('Public HTTP returned an oversized response')
        chunks=[];size=0
        try:
            if isinstance(getattr(result,'_content',None),bytes):iterator=[result.content]
            else:iterator=result.iter_content(64*1024)
            for chunk in iterator:
                resources.remaining(end)
                size+=len(chunk)
                if size>MAX_NON_STREAM_BYTES:raise ValueError('Public HTTP returned an oversized response')
                chunks.append(chunk)
            result._content=b''.join(chunks);result._content_consumed=True
        finally:result.close()
        session.close()
        return result
    except requests.RequestException:
        session.close()
        if allow_curl_fallback is True and not kwargs.get('stream'):
            return _curl_get(original_url, {**kwargs,'public_only':public_only,'guard_redirects':guard_redirects}, inherited)
        raise
    except BaseException:
        session.close()
        raise
