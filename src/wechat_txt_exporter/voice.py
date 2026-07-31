from __future__ import annotations

import json
import mimetypes
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# hf-xet reconstruction is unreliable through common Hugging Face mirrors.
# Plain HTTP downloads can resume from the same cache and behave consistently.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


LOCAL_WHISPER_MODEL = "small"
LOCAL_WHISPER_LARGE_MODEL = "large-v3"
SILICONFLOW_MODEL = "FunAudioLLM/SenseVoiceSmall"
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
AUTH_PATH = Path(__file__).resolve().parents[2] / "auth.json"
WHISPER_REPOSITORIES = {
    LOCAL_WHISPER_MODEL: "Systran/faster-whisper-small",
    LOCAL_WHISPER_LARGE_MODEL: "Systran/faster-whisper-large-v3",
}


@dataclass(slots=True)
class VoiceResult:
    transcript: str = ""
    error: str = ""


class VoiceTranscriber:
    """Decode Weixin Silk audio and transcribe it locally or via SiliconFlow."""

    def __init__(self, model_name: str = LOCAL_WHISPER_MODEL):
        self.model_name = model_name or LOCAL_WHISPER_MODEL
        if self.model_name not in {
            LOCAL_WHISPER_MODEL,
            LOCAL_WHISPER_LARGE_MODEL,
            SILICONFLOW_MODEL,
        }:
            raise ValueError(f"不支持的语音识别模型：{self.model_name}")
        self._model: Any | None = None
        self._model_load_error = ""
        self._model_load_lock = threading.Lock()
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
        if self._model_load_error:
            raise RuntimeError(self._model_load_error)
        with self._model_load_lock:
            if self._model is not None:
                return self._model
            if self._model_load_error:
                raise RuntimeError(self._model_load_error)
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]

                device = os.environ.get("WECHAT_VOICE_DEVICE", "auto").strip().lower()
                if device == "auto":
                    try:
                        import ctranslate2  # type: ignore[import-not-found]

                        device = (
                            "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
                        )
                    except Exception:
                        device = "cpu"
                compute_type = os.environ.get(
                    "WECHAT_VOICE_COMPUTE_TYPE",
                    "int8" if device == "cpu" else "float16",
                )
                try:
                    cpu_threads = max(
                        1,
                        int(
                            os.environ.get(
                                "WECHAT_VOICE_CPU_THREADS", os.cpu_count() or 4
                            )
                        ),
                    )
                except ValueError:
                    cpu_threads = os.cpu_count() or 4
                print(
                    f"正在加载 Whisper {self.model_name}（设备：{device}，"
                    f"计算精度：{compute_type}）……"
                )
                model_source = self._resolve_whisper_model()
                model = WhisperModel(
                    model_source,
                    device=device,
                    compute_type=compute_type,
                    cpu_threads=cpu_threads,
                )
            except Exception as exc:
                if isinstance(exc, ImportError):
                    message = "未安装 faster-whisper，无法进行语音转文字。"
                else:
                    message = f"Whisper 模型加载失败：{exc}"
                self._model_load_error = message
                self._fatal_error = message
                print(message)
                raise RuntimeError(message) from exc
            self._model = model
            print(
                f"Whisper {self.model_name} 加载完成（设备：{device}，"
                f"计算精度：{compute_type}，"
                f"CPU 线程：{cpu_threads}）。"
            )
            return self._model

    def _resolve_whisper_model(self) -> str:
        """Use a cached model, or download it with an endpoint fallback."""
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

        repository = WHISPER_REPOSITORIES[self.model_name]
        try:
            cached = str(snapshot_download(repository, local_files_only=True))
            if self._model_files_complete(cached):
                return cached
        except Exception:
            pass

        configured = os.environ.get("WECHAT_WHISPER_HF_ENDPOINT", "").strip()
        huggingface_endpoint = os.environ.get("HF_ENDPOINT", "").strip()
        endpoints = [
            endpoint
            for endpoint in (
                configured,
                huggingface_endpoint,
                "https://hf-mirror.com",
                "https://huggingface.co",
            )
            if endpoint
        ]
        errors: list[str] = []
        for endpoint in dict.fromkeys(endpoints):
            print(f"本地没有 Whisper {self.model_name}，正在从 {endpoint} 下载……")
            try:
                downloaded = str(
                    snapshot_download(
                        repository,
                        endpoint=endpoint,
                        etag_timeout=10,
                        max_workers=4,
                        allow_patterns=[
                            "config.json",
                            "model.bin",
                            "tokenizer.json",
                            "vocabulary.*",
                        ],
                    )
                )
                if not self._model_files_complete(downloaded):
                    raise RuntimeError("下载完成后仍缺少 config.json、model.bin 或 tokenizer.json")
                return downloaded
            except Exception as exc:
                reason = " ".join(str(exc).split())[:500] or type(exc).__name__
                errors.append(f"{endpoint}: {reason}")
                print(f"从 {endpoint} 下载失败：{reason}")
                print("正在尝试备用地址……")
        raise RuntimeError(
            "无法下载 Whisper 模型。请检查网络，或通过 "
            "WECHAT_WHISPER_HF_ENDPOINT 指定可访问的 Hugging Face 镜像。"
            + (f" 最后错误：{errors[-1]}" if errors else "")
        )

    @staticmethod
    def _model_files_complete(model_path: str) -> bool:
        path = Path(model_path)
        return all(
            (path / filename).is_file()
            for filename in ("config.json", "model.bin", "tokenizer.json")
        )

    def prepare(self) -> None:
        """Load local model once before any conversation is rewritten."""
        if self.model_name != SILICONFLOW_MODEL:
            self._load_model()

    @staticmethod
    def api_workers() -> int:
        try:
            value = int(os.environ.get("WECHAT_VOICE_API_WORKERS", "3"))
        except ValueError:
            value = 3
        return min(8, max(1, value))

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
        retry_codes = {429, 502, 503, 504}
        for attempt in range(3):
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
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                if exc.code not in retry_codes or attempt == 2:
                    raise RuntimeError(
                        f"SiliconFlow 语音识别失败（HTTP {exc.code}）：{detail}"
                    ) from exc
                retry_after = exc.headers.get("Retry-After", "").strip()
                try:
                    delay = min(10.0, max(0.0, float(retry_after)))
                except ValueError:
                    delay = float(2**attempt)
                time.sleep(delay)
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
                    str(wav_path), language="zh", vad_filter=True, beam_size=1
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
