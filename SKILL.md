---
name: media-transcriber
description: 转录视频、播客或本地音视频，生成带时间戳和说话人标签的 Markdown 逐字稿。用户要求提取口播、全文转写、修正识别错字、补标点或标注多人对话时使用。支持小红书、小宇宙、抖音、视频号、B站、YouTube 和媒体直链。已明确要转录的对话中，后续仅发媒体链接也沿用。单纯研究工具、分析链接或剪视频时不触发。
---

# 音视频转录

从真实音轨生成逐字稿，完成全文校对后保存并验证成稿。

当用户显式调用 `$media-transcriber` 时，使用本文件和同目录下的脚本执行任务。有媒体输入就开始处理，缺少输入时请用户提供链接或文件路径。

## 开始任务

1. 从分享文本提取单条 URL，保留原查询参数。多条输入按顺序去重、分别处理，单条失败后继续其余任务。相同输入或已有 `--job` 优先续跑。
2. 以本文件所在目录为技能根目录。读取本地 `config.json` 的 `runtime` 和 `output`。缺少配置时参考 [config.example.json](config.example.json)。首次运行或环境变化时用 `doctor` 检查，仅在缺少运行环境或模型时执行 `scripts/setup.ps1`。配置和恢复命令见 [运行参考](references/operations.md)。
3. 使用已配置的 Python 运行技能脚本。用户指定保存位置时传入 `--output`。

```powershell
$py = '<runtime>\.venv\Scripts\python.exe'
$entry = '<skill-dir>\scripts\transcribe.py'
& $py -X utf8 $entry run '<分享链接或本地文件>'
```

默认走公开入口。启用匿名解析时，脚本会按平台配置将公开分享链接发送给解析服务。保持匿名模式。仅在用户明确提供 Cookie 文件时使用 `--cookies`，禁止读取浏览器登录态或将 Cookie 交给解析服务。公开入口失败时报告原因，可用媒体直链或本地文件继续。遇到视频号、解析配置或获取失败时，查看[平台解析参考](references/operations.md#可选平台解析适配器)。

## 校对与导出

1. 默认使用 `--speakers auto`。只有用户或本期来源提供人数依据时才设置明确人数，并填写 `--speaker-evidence`。使用「说话人1」「说话人2」等标签，姓名归属需经用户确认或从清晰音轨核实。短应答、抢话或声纹证据不足时保留不确定标记。
2. `run` 返回 `review_pending` 后继续校对。若 `review_already_complete` 为真，直接执行 `finalize`。否则运行 `review JOB --next --count 60 --max-chars 6000`，按批次读取、核查并保存。校对由 Codex 完成，使用其他模型服务需用户明确指定。
3. 用文件工具写 UTF-8 校对 JSON，再运行 `apply-edits JOB --input FILE`。只把实际核查过的段落列入 `reviewed_ids`。每次修改记录段落 ID、完整的修改前后文本、理由与依据。保存本批后继续 `review --next`，直到没有未校对段落。[校对 JSON 格式](references/operations.md#校对与导出)
4. 执行 `finalize JOB`，需要覆盖保存位置时传入 `--output`。确认返回 `complete` 或 `existing` 且 `verified: true`，回读成稿。交付校对 Markdown、原稿、纠错记录和 SRT，说明时长与实质疑点。

后续命令沿用开始任务时的 `$py` 和 `$entry`。将 `$job` 设为 `run` 返回的 `job` 路径，将 `$editsFile` 设为本批校对 JSON 的路径。按上面的状态执行对应命令。

```powershell
& $py -X utf8 $entry review $job --next --count 60 --max-chars 6000
& $py -X utf8 $entry apply-edits $job --input $editsFile
& $py -X utf8 $entry finalize $job
```

## 校对判断

- 修正有依据的同音错字、术语、标点与断句。保留发言顺序、否定、数字、单位、重复和语气。标题、字幕和节目页可用于核对术语，逐字稿以音轨为准。
- 不确定的人名、数字和词句保留原识别，必要时复核对应音轨并标出疑点。校对护栏拒绝的修改保留原文并说明疑点。说话人标签与文字中的姓名分别核实。
- 在同一段内可按话意换段，保留段落 ID、角色和时间戳。成稿排版会按条件拼接被切片拆断的连续发言。

## 交付门槛

- `review_pending` 是待校对草稿。失败或未完成任务按状态报告，简介、评论和摘要不能替代逐字稿。
- 仅在明确试跑时使用 `--limit-seconds`，结果标为部分稿并留在任务目录。源总时长未知时，说明成稿覆盖已取得的音轨。
- 中断时继续原任务；导出中断则先用 `finalize` 恢复。遇到任务锁等待原进程结束。[断点与缓存](references/operations.md#断点与缓存)
