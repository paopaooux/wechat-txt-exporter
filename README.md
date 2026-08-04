# 微信聊天记录 TXT 导出工具

将 Windows 微信聊天记录按联系人和群聊分别导出为 UTF-8 TXT，支持语音转文字。

## 使用要求

- Windows 10/11 x64
- Windows 微信 `4.1.11.55` 或更高的 `4.1.x` x64
- Python `3.13+` x64
- 管理员权限

## 使用方法

1. 安装 Python 3.13+。安装时建议勾选 Python Launcher。
2. 双击 `run.bat`，并允许管理员权限。
3. 首次运行会自动创建 `.venv` 并安装依赖，请保持网络连接。
4. 在界面中选择微信账号和输出目录。
5. 让微信停留在登录界面，然后点击“验证数据库访问”或“一键更新全部 TXT”。
6. 日志出现“密钥捕获已就绪”后，在微信中登录所选账号。
7. 日志出现“数据库验证成功”后，等待导出完成。

数据库访问验证成功后，本次工具运行会复用密钥，接着导出时不需要再次登录。

## 语音转文字

界面默认开启语音转文字，只支持三个模型：

- `Whisper small（本地）`：本地运行，不上传语音
- `Whisper large-v3（本地）`：本地运行，准确率更高，但占用更多内存且明显更慢
- `FunAudioLLM/SenseVoiceSmall`：通过 SiliconFlow API 在线识别

本地 Whisper 模型第一次使用时会自动联网下载，其中 large-v3 的下载体积和内存占用
都远高于 small；没有 NVIDIA CUDA 时会使用 CPU，处理速度可能很慢。使用 SenseVoiceSmall 前，在项目根目录
创建 `auth.json`：

```json
{
  "api_key": "替换为你的 SiliconFlow API Key",
  "api_url": "https://api.siliconflow.cn/v1/audio/transcriptions"
}
```

`auth.json` 已加入 `.gitignore`，不要将 API Key 提交到 Git。程序启动时会联网验证该 Key；
只有验证成功，`SenseVoiceSmall（SiliconFlow API）` 才会出现在模型下拉框中。
选择 SenseVoiceSmall 后，语音会先在本地由 Silk 转为 WAV，
再发送给 SiliconFlow，并默认同时处理 3 条语音。语音数量较多时，识别可能需要较长时间；如果只想快速导出文字消息，请取消勾选“转成文字并写入 TXT”。
界面会显示当前会话的语音序号、总数、缓存复用状态和单条识别等待秒数，
长时间没有返回时也可以确认程序仍在等待模型响应。

语音性能可通过环境变量调整：`WECHAT_VOICE_DEVICE=auto|cpu|cuda`、
`WECHAT_VOICE_COMPUTE_TYPE=int8|float16`、`WECHAT_VOICE_CPU_THREADS=线程数`、
`WECHAT_VOICE_API_WORKERS=1..8`。默认会自动使用可用的 NVIDIA CUDA，API 并发数为 3；
如果 SiliconFlow 返回限流错误，可以将并发数降为 1 或 2。
Whisper 会先使用本地缓存，缺少模型时优先使用国内 Hugging Face 镜像，官方地址作为备用。
也可以通过 `WECHAT_WHISPER_HF_ENDPOINT` 指定自己的 Hugging Face 下载地址。
下载使用可断点续传的普通 HTTP 模式；重试时会复用已经下载的模型分片。

> **SiliconFlow 邀请链接**
>
> 还没有 SiliconFlow 账号？可以通过[我的邀请链接注册](https://cloud.siliconflow.cn/i/OlpmEgQx)，
> 获取 API Key 后即可在本工具中使用 SenseVoiceSmall 在线语音识别。
> 该链接为项目作者的邀请链接。

导出或语音识别过程中关闭窗口会立即退出，不再等待当前模型返回；已经完整生成的
TXT 会保留。登录密钥捕获阶段关闭时，会先安全移除微信进程断点再退出。

## 输出目录

默认输出到项目的 `exports` 目录：

```text
exports/
└─ 账号/
   ├─ 个人会话/
   └─ 群聊/
```

每个好友或群聊生成一个 TXT。第一次运行会全量导出；后续执行“一键更新全部 TXT”时，
会通过 `exports/<wxid>/.export-state.json` 检查所有会话，只重新读取并原子替换发生变化的
TXT。未变化的会话和已有语音转写会直接复用。需要忽略增量状态时，点击“强制全量重建”。
写入失败时会保留原 TXT，不会生成新的时间戳目录。

“起始日期”通过界面日历选择，例如选择 `2026-08-01` 表示只导出当天
`00:00:00` 及之后的消息。日期范围会写入账号导出文件夹，例如
`wxid_xxx（2026-08-01起）/个人会话/好友.txt`。完整导出和不同起始日期分别使用独立文件夹、
`.export-state.json` 及增量状态；以后使用相同日期更新时，只会原子替换该文件夹内变化的 TXT。
文件名优先使用备注，其次使用昵称；自己发送的消息显示为“我”。
Silk、PCM 和 WAV 仅作为转写临时文件使用，识别完成后立即删除，不会导出语音文件。

## 常见问题

### 提示未安装 Python 3.13 或更高版本

安装 Python 3.13 或更高版本的 x64 版本后重新运行 `run.bat`。如果已经安装，
请确认 Python Launcher 可用。

### 自动获取密钥失败

请确认工具以管理员身份运行，并在日志显示“密钥捕获已就绪”后登录所选账号。如果仍然失败，
请完全退出微信后重新运行工具，再按提示登录目标账号。

### 提示微信版本不支持

当前支持微信 `4.1.11.55` 及更高的 `4.1.x` 版本。跨大版本或低于最低版本的微信仍会被拦截，以免数据库结构不兼容。

### 语音转写很慢

使用本地 Whisper large-v3 时可改用 small 或 SenseVoiceSmall API，也可以关闭语音转写。

### 部分语音无法转写

微信可能已经清理本地语音数据。`media` 数据库中没有对应语音时无法恢复。

## 命令行使用

```powershell
.\.venv\Scripts\python.exe -m wechat_txt_exporter --help
.\.venv\Scripts\python.exe -m wechat_txt_exporter --account wxid_xxx
.\.venv\Scripts\python.exe -m wechat_txt_exporter --output D:\Exports
.\.venv\Scripts\python.exe -m wechat_txt_exporter --voice-transcribe --voice-model small
.\.venv\Scripts\python.exe -m wechat_txt_exporter --voice-transcribe --voice-model large-v3
.\.venv\Scripts\python.exe -m wechat_txt_exporter --since 2026-08-01
.\.venv\Scripts\python.exe -m wechat_txt_exporter --force-full
```
