# Agent 安装参考

此文件供执行 Agent 准备环境，README 不要求用户手动运行命令。技能脚本不调用 Codex、Claude 或特定客户端 API；全文校对和总结由当前 Agent 自己完成。

## 目录与权限

1. 检查是否已有同名技能，复用现有配置与环境，不创建多个活跃副本。
2. 新安装获取完整仓库到宿主支持的技能目录。没有技能注册功能时，放入独立普通目录并记录绝对路径；后续读取该处 SKILL.md。
3. 不假设存在 `.codex`、`.claude` 或某个固定盘符。整个文件夹一起安装，不能只复制 SKILL.md。
4. `agents/openai.yaml` 为可选客户端元信息，脚本不读取它。AGENTS.md 只约束开发维护，不是运行依赖。
5. 需要本地读写、子进程与网络权限。只有聊天界面的宿主不能完整运行；说明限制，不宣称已安装或转录。

## 准备环境

使用 Python 3.12、uv、FFmpeg（含 ffprobe），启用匿名解析时还需 curl。缺少系统工具时说明用途，按宿主权限规则准备；安装脚本不会自行修改全局 Python 或系统配置。

从技能根目录执行；也可给脚本传绝对路径，从任意工作目录运行：

```text
python -X utf8 scripts/setup.py --plan
python -X utf8 scripts/setup.py
```

`python` 表示当前可运行的 Python，不是固定命令名；宿主只有 uv 时可执行 `uv run --no-project --python 3.12 <skill-dir>/scripts/setup.py`。脚本本身只用标准库，会通过 uv 创建独立 Python 3.12 环境，按 requirements.txt 安装依赖，并获取模型。不会安装 Codex 或调用其命令。

- 默认运行数据和模型放在 `<skill-dir>/runtime/`，成果在 `<skill-dir>/outputs/`。
- 已有 config.json 时保留 runtime、output 和个人设置；只有显式传 `--runtime`、`--output` 才覆盖对应路径。
- 相对路径始终相对技能根目录，支持中文及空格。独立安装不要共享其他工具的环境。
- `--plan` 只显示路径与计划，不安装、不下载、不写文件。
- Windows 的 setup.ps1 作为兼容入口转调同一个 setup.py。

安装输出里的 `python` 是后续使用的解释器。也可按 config.json.runtime 定位：Windows 为 `.venv/Scripts/python.exe`，其他系统为 `.venv/bin/python`。

```text
<python> -X utf8 <skill-dir>/scripts/transcribe.py doctor
```

必须确认返回 `status: ready`。`missing` 会列出缺少的命令或模型，不能只根据退出码判断准备完成。模型已缓存时复用，不重复下载。

## 运行约定

读取 config.json，或缺省的 config.example.json，取得 runtime 和 output。把 `<python>`、`<entry>`、`<job>` 替换为绝对路径，以子进程参数数组调用，避免把分享文案拼成 shell 代码。

```text
<python> -X utf8 <entry> run <单条分享文案或文件路径>
<python> -X utf8 <entry> review <job> --next --count 60 --max-chars 6000
```

后续见 SKILL.md 与 operations.md。stdout 为 JSON，下载和模型进度在 stderr，不能混合解析。恢复任务用原 job 或原输入，不依赖特定 Agent 的会话 ID。

## 已验证范围

Windows 本地脚本与无客户端配置目录的命令调用可验证。跨客户端的技能自动发现由宿主决定；macOS/Linux 的路径分支不等于各系统真实安装已通过。遇到缺少依赖包、系统命令或权限，保留具体错误并说明，不把“支持 Agent Skills”当作所有平台已实测。
