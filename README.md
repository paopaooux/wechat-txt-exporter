# 微信聊天记录 TXT 导出工具

将 Windows 微信聊天记录按联系人和群聊分别导出为 UTF-8 TXT，支持语音转文字。

## 使用要求

- Windows 10/11 x64
- Windows 微信 `4.1.11.55` 或更高的 `4.1.x` x64
- Python `3.13+` x64
- 管理员权限

项目已经包含运行所需的 `wx_key.dll`，不需要另外下载。

## 使用方法

1. 安装 Python 3.13。安装时建议勾选 Python Launcher。
2. 双击 `run.bat`，并允许管理员权限。
3. 首次运行会自动创建 `.venv` 并安装依赖，请保持网络连接。
4. 在界面中选择微信账号和输出目录。
5. 推荐让微信先停留在未登录界面，然后点击“仅验证密钥”或“一键导出全部 TXT”。
6. 日志出现“Hook 已安装”后，在微信中登录目标账号。
7. 日志出现“数据库验证成功”后，等待导出完成。

如果开始前微信已经登录，请在 Hook 安装成功后切换账号并重新登录。仅看到“Hook 已安装”不代表已经获得密钥。

验证密钥成功后，本次工具运行会复用密钥，接着导出时不需要再次登录。

## 语音转文字

界面默认开启语音转文字，推荐使用 `small` 模型：

- `tiny`：最快，准确率较低
- `base`：速度较快
- `small`：速度和准确率较均衡，推荐
- `medium`：更慢
- `large-v3`：最慢，不建议使用普通 CPU

第一次使用某个模型时会自动联网下载。语音数量较多时，识别可能需要数小时；如果只想快速导出文字消息，请取消勾选“转成文字并写入 TXT”。

导出过程中点击关闭，工具会在当前语音处理完成后安全停止，已经生成的文件会保留。

## 输出目录

默认输出到项目的 `exports` 目录：

```text
exports/
└─ 账号/
   ├─ 个人会话/
   ├─ 群聊/
   └─ 语音/
```

每个好友或群聊生成一个 TXT。重复执行“一键导出全部 TXT”时，会重新读取全部聊天记录，
并以临时文件原子替换同名 TXT，不会生成新的时间戳目录。写入失败时会保留原 TXT。
文件名优先使用备注，其次使用昵称；自己发送的消息显示为“我”。

## 常见问题

### 提示 Python 3.13 未安装

安装 Python 3.13 x64 后重新运行 `run.bat`。如果已经安装，请确认 Python Launcher 可用。

### Hook 成功后没有反应

请在 Hook 成功后登录目标账号。账号已经登录时，需要切换账号并重新登录。

### 提示微信版本不支持

当前支持微信 `4.1.11.55` 及更高的 `4.1.x` 版本。跨大版本或低于最低版本的微信仍会被拦截，以免数据库结构不兼容。

### 语音转写很慢

将模型改为 `small`、`base` 或 `tiny`，或者关闭语音转写。

### 部分语音无法转写

微信可能已经清理本地语音数据。`media` 数据库中没有对应语音时无法恢复。

## 命令行使用

```powershell
.\.venv\Scripts\python.exe -m wechat_txt_exporter --help
.\.venv\Scripts\python.exe -m wechat_txt_exporter --account wxid_xxx
.\.venv\Scripts\python.exe -m wechat_txt_exporter --output D:\Exports
.\.venv\Scripts\python.exe -m wechat_txt_exporter --voice-transcribe --voice-model small
```
