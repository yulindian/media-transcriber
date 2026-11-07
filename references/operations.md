# 运行与恢复

```powershell
$py = '<runtime-dir>\.venv\Scripts\python.exe'
$entry = '<skill-dir>\scripts\transcribe.py'
& $py -X utf8 $entry doctor
& $py -X utf8 $entry probe '单条链接'
& $py -X utf8 $entry run '单条链接' --output '<output-dir>'
& $py -X utf8 $entry run 'D:\素材\访谈.mp4' --speakers 3 --speaker-evidence '用户明确3人'
```

配置的runtime下有 `.venv/Scripts/python.exe` 和 `models/`。模型缺失才执行 `scripts/setup.ps1`：复用官方SenseVoice int8、pyannote segmentation 3.0、CAM++缓存。环境为Python3.12、sherpa-onnx2.0.1、numpy、requests、yt-dlp与FFmpeg，无需GPU。

## 校对与导出

`run`返回job后，依次执行：

```powershell
& $py -X utf8 $entry review 'JOB绝对路径' --next --count 60 --max-chars 6000
# 用文件工具写校对JSON，不把网页或转录正文拼入shell。
& $py -X utf8 $entry apply-edits 'JOB绝对路径' --input '校对JSON绝对路径'
& $py -X utf8 $entry finalize 'JOB绝对路径' --output '<output-dir>'
```

校对格式如下。ID与全文必须来自本次review，不要照抄示例：

```json
{
  "reviewed_ids": ["t00001", "t00002"],
  "edits": [{
    "turn_id": "t00001",
    "before": "今天用极梦生成视频",
    "after": "今天用即梦生成视频。",
    "reason": "术语同音误识别，补句末标点",
    "evidence": "原始标题明确写即梦，未改变句意"
  }]
}
```

没有确定错误的已读段落只列reviewed_ids，edits可空。before/after均为完整段落。脚本检查ID、旧文、理由、依据、否定词与长度。每批保存后重复 `review --next`，只取尚未校对的段落，默认不反复返回下载直链与完整诊断数据。`--max-chars`控制字符预算但不截断单段，需详细信息可加 `--verbose`。不要先把全部ID填完。日志保留每次修改，原稿不变。finalize检查覆盖、已校对ID、硬结构、完整审计链与四个导出文件的哈希；未满足拒绝完整稿。

## 质量检查与回听

```powershell
& $py -X utf8 $entry apply-edits $job --input $editsFile --check
& $py -X utf8 $entry quality $job --count 30
& $py -X utf8 $entry quality $job --offset 30 --count 30
& $py -X utf8 $entry clip $job --turn-id 't00001' --context 3
& $py -X utf8 $entry recheck $job --turn-id 't00001' --context 3
```

`--check` 返回 `valid` 与包含段落 ID 的整批错误列表，不写入修改或已校对状态。正式保存也先执行同样预检；失败时整批不保存。不要在保存失败后自动推进下一批。

`quality` 读取本地缓存，无需下载或运行模型。`counts` 是全量计数，`issues` 是分页结果；根据 `next_offset` 与 `total_candidates` 继续。短应答、相邻音轨重叠、多候选标签均可能合理，疑点计数不等于错误数量。它不能检出所有语义错误，也不能证明多人标注正确。

`clip` 将原 WAV 对应片段及前后文保存到任务目录的 `review-clips`，返回实际时间范围、文本和说话人。默认前后各 3 秒，最大单段 120 秒。提取成功不代表回听或二次识别完成；具备音频读取能力时核实，缺少该能力时保留疑点。

`recheck` 用本地识别模型重新处理带前后文的片段，独立缓存到 `review-clips/asr-ID`。原任务的识别缓存、校对和声纹不会改变。它沿用原任务语言，音频或模型改变时使片段缓存失效。返回的 `candidate_text` 包含前后文，不能直接替换目标段落。仍需通过有依据的 `apply-edits` 记录修改；同模型重复识别不能证明候选更正确，也不确认说话人身份。每个疑点默认重识别一次，无新证据不循环重试。

Markdown 只合并同一确定说话人、无明显重叠、间隔不超过 1 秒且前句未结束的连续片段，限制合并长度；长段仅在已有标点处换段。不跨不确定角色合并，不为段内断句虚构时间戳。原稿、审计段落和 SRT 继续保留原时间切片。

成稿分别记录 `text_review_complete`、`audio_review_status`、`speaker_review_status`。后两项当前保守标为 `not_verified`，文本审阅完成不能替代人工听审。自动质量检查不是全文准确率评估。

## 断点与缓存

- 同一输入和试跑长度映射同一job；可显式 `--job` 恢复。原输入或试跑长度不同会拒绝复用。
- WAV内容哈希和识别配置组成ASR签名。JSONL保存完成切片；损坏尾行舍弃，从最后完整片段后继续。
- 声纹片段和特征存JSON/NPY；人数变更仅重聚类。配置/模型变化重做对应阶段。
- 原始段落不变才复用校对；角色重划导致段落变化则重做校对。最终文件哈希失效不视为缓存命中。
- `--limit-seconds 30` 仅明确试跑，最终门控不能把它当来源全文。
- 失败读 `manifest.json`，不要删除原素材或全局缓存。修复环境后用原命令续跑。
- 校对与导出采用可恢复事务日志。中断后重试对应命令会向前恢复；日志校验失败则停止、保留现场。进程锁防止同时写同一任务，进程退出会自动释放，不要手工删锁制造并发。
- `run --output`会记住本任务的输出目录，`finalize --output`可显式覆盖。仅计时变化不会另存重复成稿；来源、正文、角色等实质变化才重新导出。

## 可选平台解析适配器

默认匿名路线由 `config.json` 中的 `anonymous_api` 配置：

```json
{
  "anonymous_api": {
    "enabled": true,
    "endpoint": "https://api.bugpk.com/api/short_videos",
    "platforms": ["小红书", "抖音", "视频号", "B站", "YouTube"],
    "timeout": 25,
    "fallbacks": [
      {"endpoint": "https://ynvan.com/api/parse", "schema": "ynvan", "platforms": ["YouTube"]}
    ]
  }
}
```

只有允许的平台公开分享链接会发送给所配置服务，不发送 Cookie 或浏览器登录信息。每个服务最多请求一次，备用服务最多两个；原生入口与所有允许服务均失败时明确停止。设 `enabled=false` 可关闭第三方请求。

`bugpk` 协议要求返回 `code=200`、`data.type="video"` 和有效的 `data.url` 或 `data.video_url`。`ynvan` 协议读取视频描述与服务签发的媒体代理参数；需要经代理传输的内容使用 `curl` 下载，不将绑定解析服务器网络出口的媒体直链误当作本机可下载链接。服务可用性与内容权限会变化，程序不尝试绕过认证或购买解析服务。

`probe` 只检查元信息，不证明媒体可下载。`run` 下载后完整解码音轨，并在已知源时长时比较长度。成稿记录实际路线与完整性依据，不包含临时媒体签名；任务目录中的中间文件仍应保留在本地。

### 自定义命令解析器

`config.json`的resolvers按**精确域名**配置已经存在、有权使用的解析命令，优先于内置匿名路线。参数数组执行，无shell，`{url}`替换用户链接。stdout必须是UTF-8 JSON。不自动装抓包器、读取微信缓存、导出浏览器登录态或购买解析额度。

```json
{
  "resolvers": {
    "channels.weixin.qq.com": [
      "D:/已配置环境/python.exe",
      "D:/已存在的工具/resolve_media.py",
      "--url", "{url}"
    ]
  }
}
```

解析器返回：

```json
{"id":"稳定内容ID","title":"真实标题","media_url":"https://example.com/video.mp4","duration":120}
```

未配置时保持resolvers为空，使用此入口需自行准备可运行的解析器。音视频直链可直接run；本地文件可带 `--source-url 原平台链接 --title 原标题` 保留来源。cookie文件仅用 `--cookies 文件路径` 显式传入，失效只报告。

内置 HTTP 下载会在请求和跟随跳转前检查目标地址。自定义命令可以直接返回本地服务的媒体地址，但该地址的跳转目标须为公开地址。显式配置在回环地址上的匿名解析服务也适用此规则。

## 验证

`python -m unittest discover -s scripts -p 'test_*.py'` 检查签名链接、聚类、下载故障、恢复、事务、纠错和完整稿门控。doctor仅检查环境，probe仅检查元信息，不能当媒体下载或全文成功。缓存与恢复机制见 [效率与稳定性设计](../效率与稳定性设计.md)。

自动人数基于长语音加权声纹聚类与有限候选评分，标签待核对；音乐、模仿、短应答与抢话会失败。已知人数约束也不能修复所有漏分。不从文字里的性别称呼或你我关系推断并冒称声纹识别。
