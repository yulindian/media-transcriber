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

## 音轨获取与交接

```powershell
& $py -X utf8 $entry prepare '<完整分享文案、单条URL或本地文件>'
# 用 prepare 返回的 job 继续，不重复获取已验证音轨
& $py -X utf8 $entry run '<同一输入>' --job '<返回的job>'
```

`prepare` 使用与 `run` 相同的平台路由、资源预算、缓存和时长检查，只执行获取、完整解码、16kHz 单声道 PCM16 校验和非全静音检查。它不加载识别/声纹模型。`audio-receipt.json` 记录来源 ID、路线、实际时长、音轨格式、字节数、SHA-256 和是否为试跑；不包含 Cookie、签名下载地址或浏览器登录信息。它证明音轨交接结构与内容哈希，不证明作品语义、转录准确率或人工听审。

`audio_ready` 不能交付为全文；已进入校对/完成状态的同一音轨，重复 `prepare` 不会撤销已有校对状态。源素材或音轨改变后仍按现有签名与验收门槛重新核查。下载器报告的文件路径必须留在当前任务目录；用户明确提供的本地输入按其路径读取。

单条完整分享文案由脚本提取唯一 URL，Markdown 中重复显示的同一链接会去重。直接提供的 URL 保留签名转义与查询顺序；不要人工重排参数。已识别的作品 ID 用于任务归一化（B站分P单独计入），旧的精确输入任务目录继续兼容。短链和未知媒体直链保持保守区分，`--job` 可显式指定已有任务。

## 批量队列

先用文件工具创建私有输入文件：TXT 每行一条分享文案或本地路径，也可用 JSON 字符串数组。空行和 TXT 中以 `#` 开头的行会跳过。文件不超过 1 MiB，默认去重后不超过 20 项；需要更大批次时显式设置 `--max-items`（最多 100）。

```powershell
& $py -X utf8 $entry batch '<输入文件绝对路径>' --dry-run
& $py -X utf8 $entry batch '<同一文件>'
# 只排查素材获取时，不进行识别
& $py -X utf8 $entry batch '<同一文件>' --prepare-only
# 稍后明确重试失败项
& $py -X utf8 $entry batch '<同一文件>' --retry-failed
```

`--dry-run` 不创建队列或获取媒体，显示去重结果、原输入行号和输入疑点；预览中的查询值会脱敏。正常执行在 `<runtime>/queues/` 保存原输入和队列状态，此目录是私有运行数据，不应发布。相同输入文件路径、模式及试跑长度复用队列。新增项加入处理，移除项保留历史但不再执行；失败项默认保留到显式 `--retry-failed`。已经就绪的任务会重验证缓存，避免仅信任旧的完成标志。

队列串行执行并使用独立进程锁；中断时正在处理项会在下次恢复。单项失败继续其他项，总任务或磁盘预算耗尽则保留尚未处理项。`task_seconds` 在同一次批量调用中共享，不为每一项重新获得整段时限。需要 Cookie 时沿用单条任务的明确文件授权规则。

结果中的 `ready/failed/pending` 是音轨或待校对任务计数。`batch_ready` 不表示 Markdown 已生成，Agent 必须逐项完成 `run`（若只 prepare）、全文校对、质量验收及 `finalize`；不能用队列就绪数量冒充成稿数量。`batch_partial` 退出码为 1，结果仍保留全部任务状态与已成功获取的缓存。

## 错误与 Agent 协议

CLI 标准输出只包含一份 JSON 结果，模型及下载进度写入 stderr。不要将二者混合后按单个 JSON 解析。失败返回非零退出码与 `error_code/retryable/recovery_hint`；任务 `manifest.json` 还记录 `failed_stage`，下载尝试分别保存诊断。`retryable` 表示稍后可能重试，不是要求当前立即重试。

| error_code | 后续动作 |
| --- | --- |
| `invalid_input` | 检查唯一链接、文件路径或批量输入格式 |
| `dependency_missing` | 检查本工具环境、FFmpeg 和模型，运行 doctor |
| `source_unavailable` | 核对失效作品或提供有权访问的本地文件，不重复刷新 |
| `rate_limited` | 暂停该项，稍后显式重试，不连续请求解析服务 |
| `authentication_required` | 用户明确授权的 Cookie 文件、本地文件或媒体直链 |
| `access_denied` | 可能是权限或签名问题；只按既定上限刷新 |
| `network_error` | 检查网络或已有代理后续跑 |
| `resource_limit` | 检查体积、时长、磁盘和时间预算 |
| `workflow_error` | 根据失败阶段和具体提示排查 |

文本错误分类是保守提示，不是对所有平台错误语义的证明。遇到新类型错误保留现场，不能据此宣称源内容不存在或永久不可访问。

## 校对与导出

`run`返回job后，依次执行：

```powershell
& $py -X utf8 $entry review 'JOB绝对路径' --next --count 60 --max-chars 6000
# 用文件工具写校对JSON，不把网页或转录正文拼入shell。
& $py -X utf8 $entry apply-edits 'JOB绝对路径' --input '校对JSON绝对路径'
& $py -X utf8 $entry quality 'JOB绝对路径'
# 查看全部疑点并按下文格式准备验收 JSON
& $py -X utf8 $entry accept-quality 'JOB绝对路径' --input '验收JSON绝对路径'
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

数字、中文数词、日期、比例及常见中英文单位的序列发生变化时，还需要 `entity_review`。先通过 `clip` 提取当前段落，脚本会保存片段与元数据。核实音轨后，在对应 edit 中增加：

```json
"entity_review": {
  "method": "audio_excerpt_review",
  "clip_path": "<clip 返回的本任务 WAV 绝对路径>",
  "quote": "<已核实的改后句子，关键数词和单位须与 after 一致>",
  "note": "<核实依据与更改原因>"
}
```

片段必须来自本任务 `review-clips`，其音轨哈希、片段哈希和段落版本必须一致。改过正文或音轨后需重新提取。普通 `reason/evidence` 不能替代这个入口；没有能力核实音轨时保留原词。字符串护栏可能把含数词的惯用语也列为关键变更，应按证据处理，不通过删除数字规避护栏。复核记录仍是调用 Agent 的声明，脚本不会自动证明音频内容正确。

## 质量检查与回听

```powershell
& $py -X utf8 $entry apply-edits $job --input $editsFile --check
& $py -X utf8 $entry quality $job --count 30
& $py -X utf8 $entry quality $job --offset 30 --count 30
& $py -X utf8 $entry clip $job --turn-id 't00001' --context 3
& $py -X utf8 $entry recheck $job --turn-id 't00001' --context 3
```

`--check` 返回 `valid` 与包含段落 ID 的整批错误列表，不写入修改或已校对状态；预检失败退出码为 2。正式保存也先执行同样预检；失败时整批不保存。不要在保存失败后自动推进下一批。

`quality` 无需下载或运行模型，会将检查快照写入任务目录 `quality-check.json`。`counts` 是全量计数，`issues` 是分页结果；根据 `next_offset` 与 `total_candidates` 继续。短应答、相邻音轨重叠、多候选标签均可能合理，疑点计数不等于错误数量。它不能检出所有语义错误，也不能证明多人标注正确。

修正疑点后重新检查；需要保留的疑点逐段说明理由。核查完全部页后创建 UTF-8 验收 JSON：

```json
{
  "snapshot_fingerprint": "<本次 quality 返回值>",
  "accepted_issues": [
    {"turn_id": "<实际疑点ID>", "reason": "<为何可以带这个疑点交付>"}
  ]
}
```

执行 `accept-quality JOB --input FILE`。没有疑点时列表为空。每个疑点段落只出现一次，全部疑点都须处理；零时长字幕是不能接受的结构错误。检查未覆盖全部正文或缺少当前验收时不能导出。

快照绑定音轨、来源、原稿、校对稿、校对记录、关键实体复核材料和相关任务参数。任一项改变后，旧检查与验收失效。`quality_status` 分为 `accepted_no_detected_issues` 与 `accepted_with_disclosed_issues`，后者在 Markdown 和纠错记录中披露。它们都不等同于全文准确、人工听审或说话人验证。`verified` 的范围为 `artifact_integrity`。

`clip` 将原 WAV 对应片段及前后文保存到任务目录的 `review-clips`，返回实际时间范围、文本和说话人。默认前后各 3 秒，最大单段 120 秒。提取成功不代表回听或二次识别完成；具备音频读取能力时核实，缺少该能力时保留疑点。

`recheck` 用本地识别模型重新处理带前后文的片段，独立缓存到 `review-clips/asr-ID`。原任务的识别缓存、校对和声纹不会改变。它沿用原任务语言，音频或模型改变时使片段缓存失效。返回的 `candidate_text` 包含前后文，不能直接替换目标段落。仍需通过有依据的 `apply-edits` 记录修改；同模型重复识别不能证明候选更正确，也不确认说话人身份。每个疑点默认重识别一次，无新证据不循环重试。

Markdown 只合并同一确定说话人、无明显重叠、间隔不超过 1 秒且前句未结束的连续片段，限制合并长度；长段仅在已有标点处换段。不跨不确定角色合并。阅读稿省略时间轴与括号中的“待核对”编辑标记，单人直接分段、多人保留标签；不会因此修改内部正文或视为疑点已经解决。原稿、审计段落和 SRT 继续保留原时间切片与复核信息。

成稿分别记录 `text_review_complete`、`audio_review_status`、`speaker_review_status`。后两项当前保守标为 `not_verified`，文本审阅完成不能替代人工听审。自动质量检查不是全文准确率评估。

## 资源预算与来源隐私

`config.json.limits` 可覆盖下列默认值；未配置时使用同样的有限默认值。预算值须为正数。

| 字段 | 默认值 | 范围 |
| --- | --- | --- |
| `max_download_bytes` | 2147483648（2 GiB） | 远程媒体、已有媒体缓存及本地输入 |
| `max_audio_seconds` | 7200 | 单次音轨最长 2 小时，超限不伪装成完整稿 |
| `download_seconds` | 600 | requests/yt-dlp 下载预算；curl 还受 300 秒上限约束 |
| `convert_seconds` | 600 | 转换及识别前 FFmpeg 检测进程超时 |
| `metadata_seconds` | 120 | 单次 HTTP 元数据请求预算，自定义解析器另受 90 秒上限约束 |
| `task_seconds` | 14400 | 一次 run/recheck 的协作式预算，不含跨命令 Agent 校对时间 |
| `min_free_disk_bytes` | 536870912（512 MiB） | 保留磁盘余量；转换及 curl 会预留输出/下载容量 |
| `max_waveform_bytes` | 2147483648 | 波形转换内存估算上限，不含模型内存 |

下载时按块限制体积、时间和可写磁盘。转换最多尝试预算时长加 1 秒，以识别超长音轨；超限拒绝完整稿，临时输出清理，原文件和成功缓存保留。显式试跑仍严格按 `--limit-seconds` 处理。资源失败不会触发重新解析来源。

FFmpeg 和 curl 子进程有超时，原生模型推理在切片、回调和阶段边界检查总预算，不能保证强制打断正在执行的 C++ 调用。当前不是进程隔离的硬内存/硬实时服务；需要严格服务级限制时仍需外部作业进程管理。yt-dlp 的总体预算在下载进度回调处检查，抽取元信息时还受网络超时约束。

用于获取素材的完整来源保留在本地 `source.json`。导出的原稿和校对稿剥离 userinfo、签名参数和 fragment；仅允许特定平台公开作品定位参数（如 YouTube 的 `v`、B站的 `p`）。本地来源不导出个人绝对路径。公开链接因此可能需要重新登录或无法复用临时签名，不能把成稿来源当下载凭据。正文中实际说出的网址保持原样。

导出的纠错记录将片段路径改为任务相对路径，并清理理由/依据中的网址签名；完整审计仍保存在任务目录。不要把本地任务目录作为公开样本上传。

## 断点与缓存

- 同一输入和试跑长度映射同一job；可显式 `--job` 恢复。原输入或试跑长度不同会拒绝复用。
- WAV内容哈希和识别配置组成ASR签名。JSONL保存完成切片；损坏尾行舍弃，从最后完整片段后继续。
- 声纹片段和特征存JSON/NPY；人数变更仅重聚类。配置/模型变化重做对应阶段。
- 原始段落不变才复用校对；角色重划导致段落变化则重做校对。最终文件哈希失效不视为缓存命中。
- `--limit-seconds 30` 仅明确试跑，最终门控不能把它当来源全文。
- 失败读 `manifest.json`，不要删除原素材或全局缓存。修复环境后用原命令续跑。
- 校对与导出采用可恢复事务日志。中断后重试对应命令会向前恢复；日志校验失败则停止、保留现场。进程锁防止同时写同一任务，进程退出会自动释放，不要手工删锁制造并发。
- 待恢复导出也必须通过当前验收；正文、音轨或来源变化后拒绝发布旧事务。旧版本未完成的导出事务缺少新策略版本时会停止：先保留并移出任务目录的 `export-transaction.json`，然后重新检查、验收和导出。不要删除原素材或已完成的校对记录。
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
