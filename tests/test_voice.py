import sys
from types import SimpleNamespace

import pytest

import wechat_txt_exporter.voice as voice_module
from wechat_txt_exporter.voice import LOCAL_WHISPER_LARGE_MODEL, VoiceTranscriber


def test_decode_silk_uses_pysilk_file_object_api(tmp_path, monkeypatch):
    silk_path = tmp_path / "voice.silk"
    pcm_path = tmp_path / "voice.pcm"
    silk_path.write_bytes(b"#!SILK_V3\ntest")

    def fake_decode(source, target, sample_rate):
        assert hasattr(source, "read")
        assert hasattr(target, "write")
        assert source.read().startswith(b"#!SILK_V3")
        assert sample_rate == 24000
        target.write(b"\x00\x00" * 20)

    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(decode=fake_decode))

    VoiceTranscriber._decode_silk(silk_path, pcm_path)

    assert pcm_path.read_bytes() == b"\x00\x00" * 20


def test_voice_transcriber_uses_temporary_wav_and_returns_text(tmp_path, monkeypatch):
    def fake_decode(_silk_path, pcm_path, _sample_rate=24000):
        pcm_path.write_bytes(b"\x00\x00" * 240)

    class FakeModel:
        def transcribe(self, *_args, **kwargs):
            assert kwargs["beam_size"] == 1
            return iter((SimpleNamespace(text="你好"), SimpleNamespace(text="世界"))), None

    transcriber = VoiceTranscriber("small")
    monkeypatch.setattr(transcriber, "_resolve_whisper_model", lambda: "small")
    monkeypatch.setattr(transcriber, "_decode_silk", fake_decode)
    monkeypatch.setattr(transcriber, "_load_model", lambda: FakeModel())
    result = transcriber.process(b"#!SILK_V3", tmp_path)

    assert result.transcript == "你好世界"
    assert not list(tmp_path.glob(".wechat_voice_*"))


def test_voice_transcriber_can_use_sensevoice_api(tmp_path, monkeypatch):
    def fake_decode(_silk_path, pcm_path, _sample_rate=24000):
        pcm_path.write_bytes(b"\x00\x00" * 240)

    transcriber = VoiceTranscriber("FunAudioLLM/SenseVoiceSmall")
    monkeypatch.setattr(transcriber, "_decode_silk", fake_decode)
    monkeypatch.setattr(
        transcriber, "_transcribe_siliconflow", lambda _wav_path: "在线识别结果"
    )
    result = transcriber.process(b"#!SILK_V3", tmp_path)

    assert result.transcript == "在线识别结果"
    assert not list(tmp_path.glob(".wechat_voice_*"))


def test_failed_whisper_model_load_is_not_retried(monkeypatch):
    calls = 0

    class FailingModel:
        def __init__(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("model unavailable")

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=FailingModel),
    )
    transcriber = VoiceTranscriber("small")
    monkeypatch.setattr(transcriber, "_resolve_whisper_model", lambda: "small")

    with pytest.raises(RuntimeError, match="model unavailable"):
        transcriber._load_model()
    with pytest.raises(RuntimeError, match="model unavailable"):
        transcriber._load_model()

    assert calls == 1


def test_large_v3_loads_the_requested_local_model(monkeypatch):
    loaded = []

    class FakeModel:
        def __init__(self, model_name, **_kwargs):
            loaded.append(model_name)

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=FakeModel),
    )
    monkeypatch.setenv("WECHAT_VOICE_DEVICE", "cpu")

    transcriber = VoiceTranscriber(LOCAL_WHISPER_LARGE_MODEL)
    monkeypatch.setattr(
        transcriber,
        "_resolve_whisper_model",
        lambda: LOCAL_WHISPER_LARGE_MODEL,
    )
    model = transcriber._load_model()

    assert isinstance(model, FakeModel)
    assert loaded == [LOCAL_WHISPER_LARGE_MODEL]


def test_whisper_download_prefers_domestic_mirror_then_falls_back(monkeypatch):
    calls = []

    def fake_snapshot_download(_repository, **kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            raise RuntimeError("not cached")
        if kwargs.get("endpoint") == "https://hf-mirror.com":
            raise RuntimeError("mirror endpoint unavailable")
        return "cached-model"

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    monkeypatch.delenv("WECHAT_WHISPER_HF_ENDPOINT", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    monkeypatch.setattr(
        voice_module.VoiceTranscriber,
        "_model_files_complete",
        staticmethod(lambda path: path == "cached-model"),
    )

    source = voice_module.VoiceTranscriber("small")._resolve_whisper_model()

    assert source == "cached-model"
    assert [call.get("endpoint") for call in calls] == [
        None,
        "https://hf-mirror.com",
        "https://huggingface.co",
    ]
