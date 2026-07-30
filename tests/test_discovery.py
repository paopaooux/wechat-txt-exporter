from pathlib import Path

import pytest

from wechat_txt_exporter import discovery
from wechat_txt_exporter.errors import UnsupportedVersionError


def test_finds_weixin_from_registry_install_path(monkeypatch, tmp_path):
    executable = tmp_path / "Weixin.exe"
    executable.touch()
    monkeypatch.delenv("WEIXIN_EXE", raising=False)
    monkeypatch.setattr(discovery, "_registry_candidates", lambda: [executable])
    monkeypatch.setattr(discovery, "_program_files_candidates", lambda: [])

    assert discovery.find_weixin_executable() == executable.resolve()


@pytest.mark.parametrize("version", ("4.1.11.55", "4.1.12.22", "4.1.99.1"))
def test_accepts_compatible_41_versions(monkeypatch, version):
    monkeypatch.setattr(discovery, "get_file_version", lambda _path: version)

    assert discovery.verify_supported_version(Path("Weixin.exe")) == version


@pytest.mark.parametrize("version", ("4.1.11.54", "4.0.99.99", "4.2.0.0", "unknown"))
def test_rejects_versions_outside_supported_range(monkeypatch, version):
    monkeypatch.setattr(discovery, "get_file_version", lambda _path: version)

    with pytest.raises(UnsupportedVersionError):
        discovery.verify_supported_version(Path("Weixin.exe"))
