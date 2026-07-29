from types import SimpleNamespace

import sys
from types import SimpleNamespace

from wechat_txt_exporter.voice import VoiceTranscriber


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
