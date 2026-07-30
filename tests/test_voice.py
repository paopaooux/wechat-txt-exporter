import sys
from types import SimpleNamespace

from wechat_txt_exporter.voice import (
    ModelLoadProgress,
    VoiceTranscriber,
    _download_progress_class,
)


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


def test_voice_transcriber_writes_wav_and_returns_text(tmp_path, monkeypatch):
    def fake_decode(_silk_path, pcm_path, _sample_rate=24000):
        pcm_path.write_bytes(b"\x00\x00" * 240)

    class FakeModel:
        def transcribe(self, *_args, **_kwargs):
            return iter((SimpleNamespace(text="你好"), SimpleNamespace(text="世界"))), None

    transcriber = VoiceTranscriber("small")
    monkeypatch.setattr(transcriber, "_decode_silk", fake_decode)
    monkeypatch.setattr(transcriber, "_load_model", lambda: FakeModel())
    output = tmp_path / "voice.wav"
    result = transcriber.process(b"#!SILK_V3", output)

    assert result.transcript == "你好世界"
    assert result.wav_path == output
    assert output.read_bytes().startswith(b"RIFF")


def test_download_progress_class_reports_aggregate_bytes():
    events: list[ModelLoadProgress] = []
    progress_class = _download_progress_class("large-v3", events.append)
    progress = progress_class(
        total=100,
        desc="Reconstructing (incomplete total...)",
        unit="B",
        unit_scale=True,
    )
    progress.update(25)
    progress.close()

    assert events
    assert events[-1].stage == "downloading"
    assert events[-1].model_name == "large-v3"
    assert events[-1].completed == 25
    assert events[-1].total == 100


def test_load_model_reports_download_and_loading_stages(tmp_path, monkeypatch):
    events: list[ModelLoadProgress] = []
    download_calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_snapshot_download(repository, *, allow_patterns, tqdm_class, **_kwargs):
        download_calls.append((repository, tuple(allow_patterns)))
        progress = tqdm_class(
            total=100,
            desc="Reconstructing (incomplete total...)",
            unit="B",
            unit_scale=True,
        )
        progress.update(40)
        progress.update(60)
        progress.close()
        return str(tmp_path)

    class FakeWhisperModel:
        def __init__(self, model_path, *, device, compute_type):
            assert model_path == str(tmp_path)
            assert device == "cpu"
            assert compute_type == "int8"

    monkeypatch.setattr(
        "huggingface_hub.snapshot_download", fake_snapshot_download
    )
    monkeypatch.setattr("faster_whisper.WhisperModel", FakeWhisperModel)

    transcriber = VoiceTranscriber("small", progress_callback=events.append)
    model = transcriber._load_model()

    assert isinstance(model, FakeWhisperModel)
    assert download_calls[0][0] == "Systran/faster-whisper-small"
    assert "model.bin" in download_calls[0][1]
    assert [event.stage for event in events if event.stage != "downloading"] == [
        "checking",
        "loading",
        "ready",
    ]
    assert max(
        event.completed for event in events if event.stage == "downloading"
    ) == 100
