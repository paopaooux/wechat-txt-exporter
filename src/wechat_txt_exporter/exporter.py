from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .adapter import Weixin411Adapter
from .content import app_message_type, human_content
from .media import MediaResolver
from .models import Conversation, ExportResult, Message
from .voice import SILICONFLOW_MODEL, VoiceTranscriber

INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{value}" for value in range(1, 10)),
    *(f"LPT{value}" for value in range(1, 10)),
}
EXPORT_STATE_VERSION = 1
EXPORT_FORMAT_VERSION = 1
EXPORT_STATE_NAME = ".export-state.json"


def parse_since_date(value: str | None) -> tuple[str, int] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("起始日期格式应为 YYYY-MM-DD，例如 2026-08-01。") from exc
    normalized = parsed.strftime("%Y-%m-%d")
    return normalized, int(parsed.timestamp())


def _empty_export_state() -> dict[str, object]:
    return {
        "version": EXPORT_STATE_VERSION,
        "format_version": EXPORT_FORMAT_VERSION,
        "conversations": {},
        "voice_transcripts": {},
    }


def _load_export_state(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_export_state()
    if not isinstance(value, dict) or value.get("version") != EXPORT_STATE_VERSION:
        return _empty_export_state()
    if value.get("format_version") != EXPORT_FORMAT_VERSION:
        return _empty_export_state()
    if not isinstance(value.get("conversations"), dict):
        value["conversations"] = {}
    if not isinstance(value.get("voice_transcripts"), dict):
        value["voice_transcripts"] = {}
    return value


def _save_export_state(path: Path, state: dict[str, object]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _voice_cache_key(message: Message) -> str:
    identity = (
        message.conversation_id,
        message.local_id,
        message.timestamp,
        message.sort_key,
        message.raw.get("server_id", message.raw.get("msg_svr_id", "")),
    )
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()


def _previous_output_path(output_dir: Path, value: object) -> Path | None:
    if not isinstance(value, dict):
        return None
    category = value.get("category")
    filename = value.get("filename")
    if category not in {"个人会话", "群聊"} or not isinstance(filename, str):
        return None
    if Path(filename).name != filename:
        return None
    return output_dir / category / filename


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
    needs_media = base_type in {3, 43, 47, 62} or (
        base_type == 49 and app_message_type(message.content) in {6, 8, 19}
    )
    if needs_media and message.media_path is None:
        message.media_path = resolver.resolve(message.content)
    transcript = str(message.raw.get("__voice_transcript", "")).strip()
    voice_error = str(message.raw.get("__voice_error", "")).strip()
    if base_type == 34 and transcript:
        content = f"[语音转文字] {transcript}"
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
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=category_dir,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            for message in messages:
                handle.write(_format_message(message, resolver))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return len(messages)


def export_all(
    adapter: Weixin411Adapter,
    output_root: Path,
    *,
    transcribe_voice: bool = False,
    voice_model: str = "small",
    cancel_event: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
    voice_progress: Callable[[int, int, str, str], None] | None = None,
    force_full: bool = False,
    since_date: str | None = None,
) -> ExportResult:
    since = parse_since_date(since_date)
    since_label = since[0] if since is not None else ""
    since_timestamp = since[1] if since is not None else None
    export_folder_name = (
        f"{adapter.account.wxid}（{since_label}起）"
        if since_label
        else adapter.account.wxid
    )
    output_dir = output_root / export_folder_name
    output_dir.mkdir(parents=True, exist_ok=True)
    result = ExportResult(output_dir=output_dir)
    state_path = output_dir / EXPORT_STATE_NAME
    state = _load_export_state(state_path)
    conversation_states = state["conversations"]
    voice_cache = state["voice_transcripts"]
    assert isinstance(conversation_states, dict)
    assert isinstance(voice_cache, dict)
    resolver = MediaResolver(adapter.account.data_dir)
    voice_transcriber = VoiceTranscriber(voice_model) if transcribe_voice else None
    if voice_transcriber is not None:
        for stale_voice_file in output_dir.glob(".wechat_voice_*"):
            if stale_voice_file.is_file():
                stale_voice_file.unlink(missing_ok=True)
        if voice_model != SILICONFLOW_MODEL:
            if progress is not None:
                progress(f"正在准备本地语音模型：{voice_model}")
            voice_transcriber.prepare()
    voice_announced = False
    conversations = adapter.load_conversations()
    total = len(conversations)
    used_filenames: dict[bool, set[str]] = {False: set(), True: set()}
    for index, conversation in enumerate(conversations, start=1):
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            print("收到停止请求，正在安全结束导出……")
            break
        try:
            filename = _unique_filename(
                safe_filename(conversation.display_name, conversation.username),
                used_filenames[conversation.is_group],
            )
            category = "群聊" if conversation.is_group else "个人会话"
            target = output_dir / category / filename
            fingerprint = (
                adapter.conversation_fingerprint(
                    conversation, since_timestamp=since_timestamp
                )
                if since_timestamp is not None
                else adapter.conversation_fingerprint(conversation)
            )
            previous = conversation_states.get(conversation.username)
            metadata = {
                "fingerprint": fingerprint,
                "filename": filename,
                "category": category,
                "display_name": conversation.display_name,
                "transcribe_voice": transcribe_voice,
                "voice_model": voice_model if transcribe_voice else "",
                "voice_complete": True,
                "since_date": since_label,
            }
            unchanged = (
                not force_full
                and isinstance(previous, dict)
                and all(previous.get(key) == value for key, value in metadata.items())
                and (
                    target.is_file()
                    or (
                        int(previous.get("message_count", -1)) == 0
                        and not target.exists()
                    )
                )
            )
            if unchanged:
                result.unchanged += 1
                result.messages += int(previous.get("message_count", 0))
                print(f"[{index}/{total}] 未变化：{conversation.display_name}")
                if progress is not None:
                    progress(f"未变化，已跳过：{conversation.display_name}")
                continue

            print(f"[{index}/{total}] 更新：{conversation.display_name}")
            if progress is not None:
                progress(f"正在更新：{conversation.display_name}")
            messages = list(
                adapter.iter_messages(conversation, since_timestamp=since_timestamp)
                if since_timestamp is not None
                else adapter.iter_messages(conversation)
            )
            if not messages:
                result.skipped += 1
                print("  [跳过] 本地没有可导出的消息")
                metadata["message_count"] = int(
                    previous.get("message_count", 0)
                    if isinstance(previous, dict)
                    else 0
                )
                conversation_states[conversation.username] = metadata
                _save_export_state(state_path, state)
                continue
            if voice_transcriber is not None:
                voice_retry_needed = False
                voice_messages = [
                    message for message in messages if (message.message_type & 0xFFFF) == 34
                ]
                if voice_messages and not voice_announced:
                    print(f"正在准备语音识别模型：{voice_model}")
                    voice_announced = True
                voice_total = len(voice_messages)
                pending_voices: list[tuple[int, Message, bytes, str, str]] = []
                for voice_index, message in enumerate(voice_messages, start=1):
                    if cancel_event is not None and cancel_event.is_set():
                        result.cancelled = True
                        print("  收到停止请求，当前会话剩余语音不再转写。")
                        break
                    silk_data = adapter.voice_blob(conversation, message)
                    if not silk_data:
                        message.raw["__voice_error"] = "本地 media 数据库中未找到语音"
                        result.voices_failed += 1
                        if voice_progress is not None:
                            voice_progress(
                                voice_index,
                                voice_total,
                                "本地语音数据缺失",
                                conversation.display_name,
                            )
                        continue
                    cache_key = _voice_cache_key(message)
                    audio_hash = hashlib.sha256(silk_data).hexdigest()
                    cached = voice_cache.get(cache_key)
                    if (
                        isinstance(cached, dict)
                        and cached.get("model") == voice_model
                        and cached.get("audio_hash") == audio_hash
                        and isinstance(cached.get("transcript"), str)
                    ):
                        message.raw["__voice_transcript"] = cached["transcript"]
                        result.voices_cached += 1
                        if voice_progress is not None:
                            voice_progress(
                                voice_index,
                                voice_total,
                                "已复用缓存",
                                conversation.display_name,
                            )
                        continue
                    if voice_model == SILICONFLOW_MODEL:
                        pending_voices.append(
                            (voice_index, message, silk_data, cache_key, audio_hash)
                        )
                        continue
                    if progress is not None:
                        progress(f"正在转写语音：{conversation.display_name}")
                    if voice_progress is not None:
                        voice_progress(
                            voice_index,
                            voice_total,
                            "正在识别",
                            conversation.display_name,
                        )
                    voice_result = voice_transcriber.process(
                        silk_data, output_dir, cancel_event=cancel_event
                    )
                    if cancel_event is not None and cancel_event.is_set():
                        result.cancelled = True
                        print("  收到停止请求，正在立即结束导出。")
                        break
                    if voice_result.transcript:
                        message.raw["__voice_transcript"] = voice_result.transcript
                        voice_cache[cache_key] = {
                            "model": voice_model,
                            "audio_hash": audio_hash,
                            "transcript": voice_result.transcript,
                        }
                        result.voices_transcribed += 1
                        if voice_progress is not None:
                            voice_progress(
                                voice_index,
                                voice_total,
                                "识别完成",
                                conversation.display_name,
                            )
                    else:
                        message.raw["__voice_error"] = voice_result.error or "未知错误"
                        result.voices_failed += 1
                        voice_retry_needed = True
                        if voice_progress is not None:
                            voice_progress(
                                voice_index,
                                voice_total,
                                "识别失败",
                                conversation.display_name,
                            )
                if pending_voices and not result.cancelled:
                    workers = min(voice_transcriber.api_workers(), len(pending_voices))
                    if progress is not None:
                        progress(
                            f"正在并发转写语音（{workers} 路）：{conversation.display_name}"
                        )
                    already_completed = voice_total - len(pending_voices)
                    if voice_progress is not None:
                        voice_progress(
                            min(voice_total, already_completed + 1),
                            voice_total,
                            f"正在识别（{workers} 路并发）",
                            conversation.display_name,
                        )
                    with ThreadPoolExecutor(
                        max_workers=workers,
                        thread_name_prefix="voice-api",
                    ) as executor:
                        futures = {
                            executor.submit(
                                voice_transcriber.process,
                                silk_data,
                                output_dir,
                                cancel_event,
                            ): (message, cache_key, audio_hash)
                            for _, message, silk_data, cache_key, audio_hash in pending_voices
                        }
                        completed = already_completed
                        for future in as_completed(futures):
                            message, cache_key, audio_hash = futures[future]
                            completed += 1
                            if cancel_event is not None and cancel_event.is_set():
                                result.cancelled = True
                                for unfinished in futures:
                                    unfinished.cancel()
                                print("  收到停止请求，正在立即结束导出。")
                                break
                            try:
                                voice_result = future.result()
                            except Exception as exc:
                                message.raw["__voice_error"] = str(exc)
                                result.voices_failed += 1
                                voice_retry_needed = True
                                status = "识别失败"
                            else:
                                if voice_result.transcript:
                                    message.raw["__voice_transcript"] = voice_result.transcript
                                    voice_cache[cache_key] = {
                                        "model": voice_model,
                                        "audio_hash": audio_hash,
                                        "transcript": voice_result.transcript,
                                    }
                                    result.voices_transcribed += 1
                                    status = "识别完成"
                                else:
                                    message.raw["__voice_error"] = (
                                        voice_result.error or "未知错误"
                                    )
                                    result.voices_failed += 1
                                    voice_retry_needed = True
                                    status = "识别失败"
                            if voice_progress is not None:
                                voice_progress(
                                    completed,
                                    voice_total,
                                    status,
                                    conversation.display_name,
                                )
                metadata["voice_complete"] = not voice_retry_needed
            if result.cancelled:
                print("导出已停止；当前会话保留原 TXT。")
                break
            result.messages += _write_conversation(
                output_dir, conversation, messages, resolver, filename
            )
            result.succeeded += 1
            old_target = _previous_output_path(output_dir, previous)
            if old_target is not None and old_target != target:
                old_target.unlink(missing_ok=True)
            metadata["message_count"] = len(messages)
            conversation_states[conversation.username] = metadata
            _save_export_state(state_path, state)
        except Exception as exc:  # isolate individual corrupt conversations
            result.failed += 1
            result.failures.append((conversation.username, str(exc)))
            print(f"  [失败] {exc}")
    if not result.cancelled:
        _save_export_state(state_path, state)
    return result
