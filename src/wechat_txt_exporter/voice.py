from __future__ import annotations

import os
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class VoiceResult:
    wav_path: Path | None = None
    transcript: str = ""
    error: str = ""


class VoiceTranscriber:
    """Decode Weixin Silk audio and transcribe it with faster-whisper."""

    def __init__(self, model_name: str = "small"):
        self.model_name = model_name or "small"
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
            self.model_name, device=device, compute_type=compute_type
        )
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
