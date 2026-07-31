from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from .errors import KeyRecoveryError
from .models import Account


if os.name == "nt":
    class _ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


def _weixin_pids() -> list[int]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in (0, ctypes.c_void_p(-1).value):
        return []
    entry = _ProcessEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    pids: list[int] = []
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in {"weixin.exe", "wechat.exe"}:
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def recover_database_key(account: Account) -> bytes:
    from .database import (
        EncryptedDatabase,
        normalize_sqlcipher4_key,
    )

    contact_database = account.data_dir / "db_storage" / "contact" / "contact.db"

    def confirms(candidate: bytes) -> bool:
        database = EncryptedDatabase(contact_database, candidate)
        try:
            database.open()
            return True
        except Exception:
            return False
        finally:
            database.close()

    def validates_master_key(candidate: bytes) -> bool:
        normalized = normalize_sqlcipher4_key(contact_database, candidate)
        # A raw key only decrypts contact.db. The exporter needs the account
        # master key so it can derive the distinct key used by every database.
        if normalized is None or normalized == candidate:
            return False
        return confirms(candidate)

    print("  正在定位微信登录密钥函数……")
    from .debug_key import recover_from_login_debugger

    key = recover_from_login_debugger(
        _weixin_pids,
        validates_master_key,
        timeout_seconds=180,
        status=lambda message: print(f"  {message}"),
    )
    if key:
        return key
    raise KeyRecoveryError(
        "自动获取数据库密钥失败。请以管理员身份运行本工具；开始验证后，"
        "在微信中登录所选账号。"
    )
