from __future__ import annotations

import json
import mimetypes
import os
import secrets
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LOCAL_WHISPER_MODEL = "small"
SILICONFLOW_MODEL = "FunAudioLLM/SenseVoiceSmall"
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
AUTH_PATH = Path(__file__).resolve().parents[2] / "auth.json"


@dataclass(slots=True)
class VoiceResult:
    transcript: str = ""
    error: str = ""


class VoiceTranscriber:
    """Decode Weixin Silk audio and transcribe it locally or via SiliconFlow."""

    def __init__(self, model_name: str = LOCAL_WHISPER_MODEL):
        self.model_name = model_name or LOCAL_WHISPER_MODEL
        if self.model_name not in {LOCAL_WHISPER_MODEL, SILICONFLOW_MODEL}:
            raise ValueError(f"不支持的语音识别模型：{self.model_name}")
        self._model: Any | None = None
        self._fatal_error = ""

    @staticmethod
    def _decode_silk(silk_path: Path, pcm_path: Path, sample_rate: int = 24000) -> None:
        errors: list[str] = []
        try:
            import pysilk  # type: ignore[import-not-found]

            with silk_path.open("rb") as input_file, pcm_path.open("wb") as output_file:
                pysilk.decode(input_file, output_file, sample_rate)
            if pcm_path.is_file() and pcm_path.stat().st_size:
                return
        except Exception as exc:
            errors.append(f"pysilk: {exc}")

        try:
            import silk  # type: ignore[import-not-found]

            try:
                silk.decode(str(silk_path), str(pcm_path), sample_rate)
            except TypeError:
                silk.decode(str(silk_path), str(pcm_path), sample_rate=sample_rate)
            if pcm_path.is_file() and pcm_path.stat().st_size:
                return
        except Exception as exc:
            errors.append(f"silk-python: {exc}")

        try:
            import pilk  # type: ignore[import-not-found]

            try:
                pilk.decode(str(silk_path), str(pcm_path), pcm_rate=sample_rate)
            except TypeError:
                pilk.decode(str(silk_path), str(pcm_path), sample_rate)
            if pcm_path.is_file() and pcm_path.stat().st_size:
                return
        except Exception as exc:
            errors.append(f"pilk: {exc}")
        raise RuntimeError(
            "未安装可用的 Silk 解码器，请安装 silk-python（模块名 pysilk）或 pilk。"
            + (f" 诊断：{' | '.join(errors)}" if errors else "")
        )

    @staticmethod
    def _pcm_to_wav(pcm_path: Path, wav_path: Path, sample_rate: int = 24000) -> None:
        pcm = pcm_path.read_bytes()
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(pcm)

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "未安装 faster-whisper，无法进行语音转文字。"
            ) from exc
        device = os.environ.get("WECHAT_VOICE_DEVICE", "cpu")
        compute_type = os.environ.get(
            "WECHAT_VOICE_COMPUTE_TYPE", "int8" if device == "cpu" else "float16"
        )
        self._model = WhisperModel(
            LOCAL_WHISPER_MODEL, device=device, compute_type=compute_type
        )
        return self._model

    @staticmethod
    def _siliconflow_config() -> tuple[str, str]:
        try:
            value = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError("未找到 auth.json，无法调用 SiliconFlow 语音模型。") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 auth.json：{exc}") from exc
        api_key = str(value.get("api_key", "")).strip()
        api_url = str(value.get("api_url", SILICONFLOW_URL)).strip()
        if not api_key or api_key.startswith("<"):
            raise RuntimeError("请先在 auth.json 中填写 SiliconFlow API Key。")
        if not api_url.startswith("https://"):
            raise RuntimeError("auth.json 中的 api_url 必须使用 HTTPS。")
        return api_key, api_url

    @staticmethod
    def _transcribe_siliconflow(wav_path: Path) -> str:
        api_key, api_url = VoiceTranscriber._siliconflow_config()
        boundary = "----WechatTxtExporter" + secrets.token_hex(16)
        content_type = mimetypes.guess_type(wav_path.name)[0] or "audio/wav"
        audio = wav_path.read_bytes()
        body = b"".join(
            (
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="model"\r\n\r\n{SILICONFLOW_MODEL}\r\n'
                ).encode(),
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                    f"filename=\"{wav_path.name}\"\r\nContent-Type: {content_type}\r\n\r\n"
                ).encode(),
                audio,
                f"\r\n--{boundary}--\r\n".encode(),
            )
        )
        request = urllib.request.Request(
            api_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(
                f"SiliconFlow 语音识别失败（HTTP {exc.code}）：{detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"SiliconFlow 网络请求失败：{exc.reason}") from exc
        transcript = result.get("text")
        if not isinstance(transcript, str):
            raise RuntimeError(f"SiliconFlow 返回结果中没有 text 字段：{result}")
        return transcript.strip()

    def process(
        self,
        silk_data: bytes,
        work_dir: Path,
        cancel_event: threading.Event | None = None,
    ) -> VoiceResult:
        if self._fatal_error:
            return VoiceResult(error=self._fatal_error)
        if not silk_data:
            return VoiceResult(error="本地语音数据为空")
        temporary_paths: list[Path] = []
        try:
            work_dir.mkdir(parents=True, exist_ok=True)

            def temporary_path(suffix: str) -> Path:
                with tempfile.NamedTemporaryFile(
                    prefix=".wechat_voice_",
                    suffix=suffix,
                    dir=work_dir,
                    delete=False,
                ) as temporary_file:
                    path = Path(temporary_file.name)
                temporary_paths.append(path)
                return path

            silk_path = temporary_path(".silk")
            pcm_path = temporary_path(".pcm")
            wav_path = temporary_path(".wav")
            silk_path.write_bytes(silk_data)
            self._decode_silk(silk_path, pcm_path)
            self._pcm_to_wav(pcm_path, wav_path)

            if cancel_event is not None and cancel_event.is_set():
                return VoiceResult(error="语音识别已取消")

            if self.model_name == SILICONFLOW_MODEL:
                transcript = self._transcribe_siliconflow(wav_path)
            else:
                model = self._load_model()
                segments, _info = model.transcribe(
                    str(wav_path), language="zh", vad_filter=True, beam_size=5
                )
                parts: list[str] = []
                for segment in segments:
                    if cancel_event is not None and cancel_event.is_set():
                        return VoiceResult(error="语音识别已取消")
                    parts.append(str(segment.text).strip())
                transcript = "".join(parts).strip()
            if not transcript:
                return VoiceResult(error="语音识别结果为空")
            return VoiceResult(transcript=transcript)
        except Exception as exc:
            message = str(exc)
            if "未安装" in message or "auth.json" in message or "API Key" in message:
                self._fatal_error = message
            return VoiceResult(error=message)
        finally:
            for path in temporary_paths:
                path.unlink(missing_ok=True)


def validate_siliconflow_api_key(timeout: float = 10) -> tuple[bool, str]:
    """Validate the configured key without uploading any user audio."""
    try:
        api_key, api_url = VoiceTranscriber._siliconflow_config()
        parsed = urllib.parse.urlsplit(api_url)
        models_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "/v1/models", "", "")
        )
        request = urllib.request.Request(
            models_url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
        return True, "SiliconFlow API Key 验证成功。"
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return False, "SiliconFlow API Key 无效或无权访问。"
        return False, f"SiliconFlow Key 验证失败（HTTP {exc.code}）。"
    except urllib.error.URLError as exc:
        return False, f"无法连接 SiliconFlow 验证 Key：{exc.reason}"
    except Exception as exc:
        return False, str(exc)
