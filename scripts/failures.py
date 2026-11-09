"""Actionable failure categories; no automatic login, sleeps or provider changes."""
import re
import resources


class WorkflowError(ValueError):
    def __init__(self,message,diagnostic):
        super().__init__(message);self.diagnostic=dict(diagnostic)


def describe(error):
    if isinstance(error,WorkflowError):return dict(error.diagnostic)
    text=re.sub(r'https?://\S+','',str(error)).lower()
    code='workflow_error';hint='查看任务 manifest.json 的阶段和错误，修复后用原任务续跑。';retry=False
    if isinstance(error,resources.ResourceLimit):code='resource_limit';hint='保留缓存，检查时长、体积、时间和磁盘预算后续跑。'
    elif isinstance(error,FileNotFoundError) or re.search(r'(ffmpeg|ffprobe|yt-dlp|模型).*(not found|missing|不存在|缺失)',text):
        code='dependency_missing';hint='检查本工具 Python 环境、FFmpeg 和模型文件，运行 doctor，按技能说明初始化缺失依赖。'
    elif re.search(r'\b(404|410)\b|not found|has been removed|video unavailable|已删除|不存在',text):
        code='source_unavailable';hint='来源可能已失效；核对作品链接，或提供有权访问的本地文件。'
    elif re.search(r'\b429\b|too many requests|rate.limit|限流|请求过于频繁',text):
        code='rate_limited';hint='平台正在限流，本次停止该项；稍后显式重试，不循环请求。';retry=True
    elif re.search(r'\b401\b|login required|sign in|authentication required|不是公开资源|需登录',text):
        code='authentication_required';hint='需要有效授权；可提供明确授权的 Cookie 文件、本地文件或媒体直链，不读取浏览器登录态。'
    elif re.search(r'\b403\b|forbidden|access denied',text):
        code='access_denied';hint='访问受限或媒体签名失效；仅按既定上限刷新，仍失败时使用本地文件。'
    elif re.search(r'timed out|timeout|connection|网络|连接|超时',text):
        code='network_error';hint='检查网络或已有代理，保留任务，稍后重试。';retry=True
    elif re.search(r'输入为空|单条链接|链接无效|账号凭据|批量 json|批量输入|输入文件|max-items',text):
        code='invalid_input';hint='提供一条分享链接或可读本地路径；多条使用 batch。'
    return dict(error_code=code,retryable=retry,recovery_hint=hint)


def should_stop_refresh(error):
    return describe(error)['error_code'] in ('source_unavailable','rate_limited','authentication_required','resource_limit')
