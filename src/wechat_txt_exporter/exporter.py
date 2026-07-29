from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path

from .adapter import Weixin411Adapter
from .content import app_message_type, human_content
from .media import MediaResolver
from .models import Conversation, ExportResult, Message
from .voice import VoiceTranscriber

INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{value}" for value in range(1, 10)),
    *(f"LPT{value}" for value in range(1, 10)),
}


def safe_filename(display_name: str, username: str, limit: int = 180) -> str:
    cleaned = INVALID_FILENAME_RE.sub("_", display_name).strip(" .") or "未命名会话"
    if cleaned.upper() in WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    cleaned = cleaned[: max(1, limit - 4)].rstrip(" .") or "未命名会话"
    return f"{cleaned}.txt"


def _unique_filename(filename: str, used: set[str]) -> str:
    candidate = filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    number = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({number}){suffix}"
        number += 1
    used.add(candidate.casefold())
    return candidate


def _format_message(message: Message, resolver: MediaResolver) -> str:
    base_type = message.message_type & 0xFFFF
    needs_media = base_type in {3, 34, 43, 47, 62} or (
        base_type == 49 and app_message_type(message.content) in {6, 8, 19}
    )
    if needs_media and message.media_path is None:
        message.media_path = resolver.resolve(message.content)
    transcript = str(message.raw.get("__voice_transcript", "")).strip()
    voice_error = str(message.raw.get("__voice_error", "")).strip()
    if base_type == 34 and transcript:
        content = f"[语音转文字] {transcript}"
        if message.media_path:
            content += f"\n[语音文件] {message.media_path}"
    else:
        content = human_content(message.message_type, message.content, message.media_path)
        if base_type == 34 and voice_error:
            content += f"（转写失败：{voice_error}）"
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = content.replace("\n", "\n    ")
    try:
        timestamp = datetime.fromtimestamp(message.timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        timestamp = "时间未知"
    return f"[{timestamp}] {message.sender_name}：{content}\n"


def _write_conversation(
    output_dir: Path,
    conversation: Conversation,
    messages: list[Message],
    resolver: MediaResolver,
    filename: str | None = None,
) -> int:
    filename = filename or safe_filename(conversation.display_name, conversation.username)
    category_dir = output_dir / ("群聊" if conversation.is_group else "个人会话")
    category_dir.mkdir(parents=True, exist_ok=True)
    target = category_dir / filename
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for message in messages:
            handle.write(_format_message(message, resolver))
    return len(messages)


def export_all(
    adapter: Weixin411Adapter,
    output_root: Path,
    *,
    transcribe_voice: bool = False,
    voice_model: str = "small",
    cancel_event: threading.Event | None = None,
) -> ExportResult:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = output_root / f"{timestamp}_{adapter.account.wxid}"
    suffix = 1
    while output_dir.exists():
        output_dir = output_root / f"{timestamp}_{adapter.account.wxid}_{suffix}"
        suffix += 1
    output_dir.mkdir(parents=True, exist_ok=False)
    result = ExportResult(output_dir=output_dir)
    resolver = MediaResolver(adapter.account.data_dir)
    voice_transcriber = VoiceTranscriber(voice_model) if transcribe_voice else None
    voice_announced = False
    conversations = adapter.load_conversations()
    total = len(conversations)
    used_filenames: dict[bool, set[str]] = {False: set(), True: set()}
    for index, conversation in enumerate(conversations, start=1):
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            print("收到停止请求，正在安全结束导出……")
            break
        print(f"[{index}/{total}] 导出：{conversation.display_name}")
        try:
            messages = list(adapter.iter_messages(conversation))
            if not messages:
                result.skipped += 1
                print("  [跳过] 本地没有可导出的消息")
                continue
            if voice_transcriber is not None:
                voice_messages = [
                    message for message in messages if (message.message_type & 0xFFFF) == 34
                ]
                if voice_messages and not voice_announced:
                    print(f"正在加载语音识别模型：{voice_model}（首次使用可能需要下载）")
                    voice_announced = True
                category = "群聊" if conversation.is_group else "个人会话"
                conversation_dir = (
                    output_dir
                    / "语音"
                    / category
                    / Path(safe_filename(conversation.display_name, conversation.username)).stem
                )
                for message in voice_messages:
                    if cancel_event is not None and cancel_event.is_set():
                        result.cancelled = True
                        print("  收到停止请求，当前会话剩余语音不再转写。")
                        break
                    silk_data = adapter.voice_blob(conversation, message)
                    if not silk_data:
                        message.raw["__voice_error"] = "本地 media 数据库中未找到语音"
                        result.voices_failed += 1
                        continue
                    wav_path = conversation_dir / f"{message.timestamp}_{message.local_id}.wav"
                    voice_result = voice_transcriber.process(silk_data, wav_path)
                    if voice_result.wav_path:
                        message.media_path = voice_result.wav_path.resolve()
                    if voice_result.transcript:
                        message.raw["__voice_transcript"] = voice_result.transcript
                        result.voices_transcribed += 1
                    else:
                        message.raw["__voice_error"] = voice_result.error or "未知错误"
                        result.voices_failed += 1
                        if not voice_result.wav_path:
                            silk_path = wav_path.with_suffix(".silk")
                            silk_path.parent.mkdir(parents=True, exist_ok=True)
                            silk_path.write_bytes(silk_data)
                            message.media_path = silk_path.resolve()
            filename = _unique_filename(
                safe_filename(conversation.display_name, conversation.username),
                used_filenames[conversation.is_group],
            )
            result.messages += _write_conversation(
                output_dir, conversation, messages, resolver, filename
            )
            result.succeeded += 1
            if result.cancelled:
                print("导出已安全停止；已完成的 TXT 和语音文件均已保留。")
                break
        except Exception as exc:  # isolate individual corrupt conversations
            result.failed += 1
            result.failures.append((conversation.username, str(exc)))
            print(f"  [失败] {exc}")
    return result
