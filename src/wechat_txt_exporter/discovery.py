from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from pathlib import Path

from .errors import DiscoveryError, UnsupportedVersionError
from .models import Account

MIN_SUPPORTED_VERSION = "4.1.11.55"
SUPPORTED_VERSION_FAMILY = (4, 1)
ACCOUNT_DIR_RE = re.compile(r"^(?P<wxid>wxid_.+?)(?:_(?P<suffix>[0-9a-fA-F]{4}))?$")


def _program_files_candidates() -> list[Path]:
    values = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    candidates: list[Path] = []
    for value in values:
        if not value:
            continue
        base = Path(value)
        candidates.extend(
            [base / "Tencent" / "Weixin" / "Weixin.exe", base / "Tencent" / "WeChat" / "WeChat.exe"]
        )
    return candidates


def _registry_candidates() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    candidates: list[Path] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_name in (
            r"Software\Tencent\Weixin",
            r"Software\WOW6432Node\Tencent\Weixin",
        ):
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    install_path, _ = winreg.QueryValueEx(key, "InstallPath")
            except OSError:
                continue
            if install_path:
                candidates.append(Path(str(install_path)) / "Weixin.exe")
    return candidates


def find_weixin_executable() -> Path:
    override = os.environ.get("WEIXIN_EXE")
    candidates = (
        ([Path(override)] if override else [])
        + _registry_candidates()
        + _program_files_candidates()
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise DiscoveryError("未找到 Weixin.exe；可通过环境变量 WEIXIN_EXE 指定完整路径。")


def get_file_version(path: Path) -> str:
    if os.name != "nt":
        raise DiscoveryError("该工具仅支持 Windows。")
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise DiscoveryError(f"无法读取微信版本：{path}")
    buffer = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise DiscoveryError(f"无法读取微信版本：{path}")
    value = ctypes.c_void_p()
    length = wintypes.UINT()
    if not ctypes.windll.version.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
        raise DiscoveryError(f"微信版本信息损坏：{path}")
    words = ctypes.cast(value, ctypes.POINTER(wintypes.DWORD * 13)).contents
    ms, ls = words[2], words[3]
    return f"{ms >> 16}.{ms & 0xffff}.{ls >> 16}.{ls & 0xffff}"


def verify_supported_version(executable: Path) -> str:
    version = get_file_version(executable)
    try:
        parts = tuple(int(part) for part in version.split("."))
        minimum = tuple(int(part) for part in MIN_SUPPORTED_VERSION.split("."))
    except ValueError:
        parts = ()
        minimum = ()
    if (
        len(parts) != 4
        or parts[:2] != SUPPORTED_VERSION_FAMILY
        or parts < minimum
    ):
        raise UnsupportedVersionError(
            f"当前微信版本为 {version}；支持 {MIN_SUPPORTED_VERSION} 及更高的 4.1.x 版本。"
        )
    return version


def find_data_root() -> Path:
    override = os.environ.get("XWECHAT_FILES")
    if override:
        root = Path(override).expanduser()
        if root.is_dir():
            return root.resolve()
        raise DiscoveryError(f"XWECHAT_FILES 指向的目录不存在：{root}")

    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    config_dir = appdata / "Tencent" / "xwechat" / "config"
    if config_dir.is_dir():
        for ini in sorted(config_dir.glob("*.ini"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                configured_home = Path(ini.read_text(encoding="utf-8-sig").strip())
            except (OSError, UnicodeError):
                continue
            candidate = configured_home / "xwechat_files"
            if candidate.is_dir():
                return candidate.resolve()

    candidate = Path.home() / "xwechat_files"
    if candidate.is_dir():
        return candidate.resolve()
    raise DiscoveryError("未找到 xwechat_files 数据目录；可通过环境变量 XWECHAT_FILES 指定。")


def discover_accounts(data_root: Path) -> list[Account]:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    app_login_root = appdata / "Tencent" / "xwechat" / "login"
    accounts: list[Account] = []
    for entry in data_root.iterdir():
        if not entry.is_dir() or not (entry / "db_storage").is_dir():
            continue
        match = ACCOUNT_DIR_RE.match(entry.name)
        if not match:
            continue
        wxid = match.group("wxid")
        login_candidates = [app_login_root / wxid, data_root / "all_users" / "login" / wxid]
        login_dir = next((path for path in login_candidates if path.is_dir()), None)
        accounts.append(
            Account(wxid=wxid, data_dir=entry.resolve(), login_dir=login_dir, suffix=match.group("suffix") or "")
        )
    if not accounts:
        raise DiscoveryError(f"在 {data_root} 下没有发现微信 4.x 账号数据。")
    return sorted(accounts, key=lambda a: a.data_dir.stat().st_mtime, reverse=True)


def select_account(accounts: list[Account], requested: str | None = None) -> Account:
    if requested:
        normalized = requested.strip().lower()
        matches = [
            account
            for account in accounts
            if account.wxid.lower() == normalized or account.data_dir.name.lower() == normalized
        ]
        if len(matches) == 1:
            return matches[0]
        raise DiscoveryError(f"没有找到账号：{requested}")
    if len(accounts) == 1:
        return accounts[0]

    print("检测到多个微信账号：")
    for index, account in enumerate(accounts, start=1):
        modified = account.data_dir.stat().st_mtime
        from datetime import datetime

        modified_text = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
        print(f"  {index}. {account.wxid}（最近使用：{modified_text}）")
    while True:
        answer = input("请选择要导出的账号编号：").strip()
        try:
            return accounts[int(answer) - 1]
        except (ValueError, IndexError):
            print(f"请输入 1 到 {len(accounts)} 之间的数字。")
