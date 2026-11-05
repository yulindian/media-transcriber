# y0-media-transcriber

把视频、播客或本地录音转成带时间戳和说话人标签的 Markdown。语音识别在本机运行，支持 Windows CPU。通过 Codex 技能使用时，Codex 会逐段校对转录稿，修正有依据的错字，补标点和分段。

## 交给 Agent 安装和调用

第一次使用时，把下面这段发给有本机终端权限的 Codex Agent，并替换最后一行。

```text
安装并调用 media-transcriber 技能处理下面的音视频。
技能仓库是 https://github.com/yulindian/y0-media-transcriber.git。
先检查本机是否已安装 media-transcriber。已安装就读取现有 SKILL.md 并复用配置。
未安装时，读取仓库 README.md，按“手动安装”放入 Codex 技能目录并初始化环境，再读取 SKILL.md。
按 SKILL.md 执行完整转录、说话人标注、逐批校对和验证导出，最后给我本地成稿链接。
输入：<单条分享链接或本地文件路径>
```

以后直接调用 `$media-transcriber`，附上分享链接或本地文件路径。如果当前会话还未发现刚安装的技能，可让 Agent 读取本机技能目录中的 `media-transcriber/SKILL.md`，按文件中的命令完成当前任务。

## 手动安装

**1. 安装。** 在 Windows PowerShell 中准备 Git、uv、FFmpeg（含 `ffprobe`）和 `curl.exe`。克隆需要 GitHub 仓库访问权限。安装脚本用 uv 创建 Python 3.12 环境，首次运行会下载模型。

```powershell
$skillDir = Join-Path $env:USERPROFILE '.codex\skills\media-transcriber'
git clone https://github.com/yulindian/y0-media-transcriber.git $skillDir
Set-Location -LiteralPath $skillDir
.\scripts\setup.ps1
```

使用自定义 Codex 技能目录时，将 `$skillDir` 改为对应路径。首次安装会生成 `config.json`，成稿默认写入「文档」下的「音视频转录」。可修改其中的 `output`。

**2. 在 Codex 中发链接或本地文件。** 技能调用名是 `$media-transcriber`。

```text
用 $media-transcriber 完整转录这条链接，标注说话人，修正有依据的错字，
补标点并按句意分段，保存为 Markdown。
<单条分享链接或本地文件路径>
```

同一任务中断后，可以用原链接继续。长播客会分批校对，最后统一导出。

## 输出示例

```markdown
[00:00:04] **说话人1**：我们先聊聊这个项目是怎么开始的。

[00:00:09] **说话人2**：最初只是想把访谈录音整理成可以搜索的文字。
```

上面是格式示例。完整导出包含四个文件：

| 文件 | 用途 |
| --- | --- |
| `.md` | 校对稿，保留来源、时间戳、说话人和自然段 |
| `.原始.md` | 原始识别稿 |
| `.纠错记录.json` | 记录修改前后的文字和依据 |
| `.srt` | 带说话人标签的字幕 |

说话人编号来自声纹估计。多人重叠、短应答和背景音乐可能影响归属，已知人数可作为约束。

## 支持的输入

| 输入 | 获取方式 |
| --- | --- |
| 小红书视频 | 展开分享短链，通过 yt-dlp 获取，可使用已配置的匿名解析备用 |
| 小宇宙公开单集 | 从节目页读取音频地址 |
| 抖音视频 | 优先查询公开作品接口，备用入口为 yt-dlp 和匿名解析 |
| 视频号 | 通过配置的解析服务获取，无需在微信中逐条播放 |
| B站、YouTube | 优先使用 yt-dlp，可按配置使用匿名解析 |
| 音视频直链、本地文件 | 直接下载或读取 FFmpeg 可识别的音轨 |

默认配置按需向第三方解析服务发送分享链接。可以在 `config.json` 中将 `anonymous_api.enabled` 设为 `false`。视频号分享链接依赖该服务或自定义解析器。私有、付费或受平台限制的内容可能无法获取，可以改用有权访问的媒体直链或本地文件。

## 命令行

从仓库根目录运行。也可将仓库克隆到任意目录，仅使用 CLI。

```powershell
$config = Get-Content -LiteralPath .\config.json -Raw -Encoding utf8 | ConvertFrom-Json
$py = Join-Path $config.runtime '.venv\Scripts\python.exe'
& $py -X utf8 .\scripts\transcribe.py doctor
& $py -X utf8 .\scripts\transcribe.py run 'C:\Media\interview.wav'
```

`run` 生成原始稿并返回任务目录。CLI 用户需通过 `review → apply-edits → finalize` 完成校对和导出，Codex 技能会执行这套流程。

- [使用指南](使用方案.md)涵盖配置、校对示例和故障排查。
- [命令与恢复参考](references/operations.md)说明参数、缓存、导出和自定义解析器。
- [效率与稳定性设计](效率与稳定性设计.md)介绍处理机制与开发检查。
