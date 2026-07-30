from __future__ import annotations

import io
import os
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MODEL_REPOSITORIES = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}
MODEL_ALLOW_PATTERNS = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
)


@dataclass(slots=True)
class VoiceResult:
    wav_path: Path | None = None
    transcript: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class ModelLoadProgress:
    stage: str
    model_name: str
    completed: int = 0
    total: int = 0
    detail: str = ""


ModelProgressCallback = Callable[[ModelLoadProgress], None]


def _download_progress_class(
    model_name: str, callback: ModelProgressCallback
) -> type[Any]:
    """Build a silent tqdm class that forwards aggregate model bytes."""
    from tqdm.auto import tqdm

    class DownloadProgress(tqdm):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            description = str(kwargs.get("desc", ""))
            self._tracks_model_bytes = (
                kwargs.get("unit") == "B"
                and description.casefold().startswith("reconstruct")
            )
            kwargs["disable"] = False
            kwargs["file"] = io.StringIO()
            kwargs.setdefault("mininterval", 0.1)
            super().__init__(*args, **kwargs)
            self._report()

        def _report(self) -> None:
            if not self._tracks_model_bytes:
                return
            callback(
                ModelLoadProgress(
                    stage="downloading",
                    model_name=model_name,
                    completed=max(0, int(getattr(self, "n", 0) or 0)),
                    total=max(0, int(getattr(self, "total", 0) or 0)),
                )
            )

        def display(self, *args: Any, **kwargs: Any) -> None:
            # The GUI renders progress. Do not emit terminal control sequences.
            return None

        def refresh(self, *args: Any, **kwargs: Any) -> bool:
            self._report()
            return True

        def close(self) -> None:
            self._report()
            super().close()

    return DownloadProgress


class VoiceTranscriber:
    """Decode Weixin Silk audio and transcribe it with faster-whisper."""

    def __init__(
        self,
        model_name: str = "small",
        progress_callback: ModelProgressCallback | None = None,
    ):
        self.model_name = model_name or "small"
        self.progress_callback = progress_callback
        self._model: Any | None = None
        self._fatal_error = ""

    def _notify_progress(
        self,
        stage: str,
        *,
        completed: int = 0,
        total: int = 0,
        detail: str = "",
    ) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(
                ModelLoadProgress(
                    stage=stage,
                    model_name=self.model_name,
                    completed=completed,
                    total=total,
                    detail=detail,
                )
            )
        except Exception:
            # Progress reporting must never interrupt an export.
            pass

    def _download_model(self) -> str:
        from huggingface_hub import snapshot_download

        model_path = Path(self.model_name).expanduser()
        if model_path.is_dir():
            return str(model_path.resolve())

        repository = MODEL_REPOSITORIES.get(self.model_name)
        if repository is None and "/" in self.model_name:
            repository = self.model_name
        if repository is None:
            # Let faster-whisper produce its normal validation error.
            return self.model_name

        progress_class = _download_progress_class(
            self.model_name, self.progress_callback or (lambda _progress: None)
        )
        return str(
            snapshot_download(
                repository,
                allow_patterns=list(MODEL_ALLOW_PATTERNS),
                library_name="faster-whisper",
                tqdm_class=progress_class,
            )
        )

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
            error = RuntimeError(
                "未安装 faster-whisper，无法进行语音转文字。"
            )
            self._notify_progress("error", detail=str(error))
            raise error from exc
        device = os.environ.get("WECHAT_VOICE_DEVICE", "cpu")
        compute_type = os.environ.get(
            "WECHAT_VOICE_COMPUTE_TYPE", "int8" if device == "cpu" else "float16"
        )
        try:
            model_source = self.model_name
            if self.progress_callback is not None:
                self._notify_progress("checking")
                model_source = self._download_model()
                self._notify_progress("loading")
            self._model = WhisperModel(
                model_source, device=device, compute_type=compute_type
            )
        except Exception as exc:
            self._notify_progress("error", detail=str(exc))
            raise
        self._notify_progress("ready")
        return self._model

    def process(self, silk_data: bytes, wav_path: Path) -> VoiceResult:
        if self._fatal_error:
            return VoiceResult(error=self._fatal_error)
        if not silk_data:
            return VoiceResult(error="本地语音数据为空")
        try:
            with tempfile.TemporaryDirectory(prefix="wechat_voice_") as temp:
                temp_dir = Path(temp)
                silk_path = temp_dir / "voice.silk"
                pcm_path = temp_dir / "voice.pcm"
                silk_path.write_bytes(silk_data)
                self._decode_silk(silk_path, pcm_path)
                self._pcm_to_wav(pcm_path, wav_path)

            model = self._load_model()
            segments, _info = model.transcribe(
                str(wav_path), language="zh", vad_filter=True, beam_size=5
            )
            transcript = "".join(str(segment.text).strip() for segment in segments).strip()
            if not transcript:
                return VoiceResult(wav_path=wav_path, error="语音识别结果为空")
            return VoiceResult(wav_path=wav_path, transcript=transcript)
        except Exception as exc:
            message = str(exc)
            if "未安装" in message:
                self._fatal_error = message
            return VoiceResult(
                wav_path=wav_path if wav_path.is_file() else None, error=message
            )
