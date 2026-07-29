from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sqlite3
from ctypes import wintypes
from pathlib import Path
from collections.abc import Callable
from typing import Iterable

from .errors import KeyRecoveryError
from .models import Account

HEX_KEY_RE = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
HEX_LITERAL_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
V4_BINARY_SENTINEL = b"\x20\x66\x74\x73\x35\x28\x25\x00"


def _parse_key_text(value: str) -> bytes:
    clean = value.strip().removeprefix("0x").replace(" ", "")
    if len(clean) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", clean):
        raise KeyRecoveryError("密钥文件必须只包含 64 个十六进制字符。")
    return bytes.fromhex(clean)


def load_manual_key(path: Path) -> bytes:
    try:
        return _parse_key_text(path.read_text(encoding="ascii"))
    except OSError as exc:
        raise KeyRecoveryError(f"无法读取密钥文件：{path}") from exc


def _key_info_database(account: Account, data_root: Path) -> Path:
    path = data_root / "all_users" / "login" / account.wxid / "key_info.db"
    if path.is_file():
        return path
    raise KeyRecoveryError(f"未找到账号密钥索引：{path}")


def load_key_records(account: Account, data_root: Path) -> list[tuple[str, bytes]]:
    path = _key_info_database(account, data_root)
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        rows = connection.execute(
            "SELECT user_name_md5, key_md5, key_info_data FROM LoginKeyInfoTable"
        ).fetchall()
        connection.close()
    except sqlite3.Error as exc:
        raise KeyRecoveryError(f"无法读取本地密钥索引：{exc}") from exc

    account_hashes = {
        hashlib.md5(account.wxid.encode("utf-8")).hexdigest(),
        hashlib.md5(account.data_dir.name.encode("utf-8")).hexdigest(),
    }
    def text_value(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("ascii", errors="ignore")
        return str(value or "")

    matched = [row for row in rows if text_value(row[0]).lower() in account_hashes]
    source = matched or rows
    records: list[tuple[str, bytes]] = []
    for _user_hash, key_hash, blob in source:
        normalized = text_value(key_hash).lower()
        if isinstance(blob, bytes) and (not normalized or re.fullmatch(r"[0-9a-f]{32}", normalized)):
            records.append((normalized, blob))
    if not records:
        raise KeyRecoveryError("密钥索引中没有可用记录。")
    current_blob: bytes | None = None
    if account.login_dir:
        current_path = account.login_dir / "key_info.dat"
        try:
            current_blob = current_path.read_bytes()
        except OSError:
            pass
    records.sort(key=lambda item: item[1] == current_blob, reverse=True)
    return records[:16]


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_unprotect(data: bytes) -> bytes | None:
    if os.name != "nt" or not data:
        return None
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        return None
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _first_length_delimited_field(blob: bytes) -> bytes | None:
    if not blob or blob[0] != 0x0A:
        return None
    value = 0
    shift = 0
    index = 1
    while index < len(blob) and shift <= 28:
        byte = blob[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            end = index + value
            return blob[index:end] if end <= len(blob) else None
        shift += 7
    return None


def _matches_hash(candidate: bytes, expected: set[str]) -> bool:
    if len(candidate) != 32:
        return False
    raw_hash = hashlib.md5(candidate).hexdigest()
    hex_hash = hashlib.md5(candidate.hex().encode("ascii")).hexdigest()
    return raw_hash in expected or hex_hash in expected


def _candidate_keys(data: bytes) -> Iterable[bytes]:
    if len(data) == 32:
        yield data
    stripped = data.strip().removeprefix(b"0x")
    if len(stripped) == 64 and re.fullmatch(rb"[0-9a-fA-F]{64}", stripped):
        yield bytes.fromhex(stripped.decode("ascii"))
    # WCDB may retain x'<key><salt>' rather than a standalone 64-hex value.
    # Take the first 32 bytes exactly as wxkey does.
    for match in HEX_LITERAL_RE.finditer(data):
        yield bytes.fromhex(match.group(1)[:64].decode("ascii"))
    for match in HEX_KEY_RE.finditer(data):
        yield bytes.fromhex(match.group().decode("ascii"))


def recover_with_dpapi(
    records: list[tuple[str, bytes]], validator: Callable[[bytes], bool] | None = None
) -> bytes | None:
    expected = {key_hash for key_hash, _ in records if key_hash}
    for _key_hash, blob in records:
        payload = _first_length_delimited_field(blob)
        inputs = [blob]
        if payload:
            inputs.append(payload)
            for offset in (4, 8, 12, 16, 20, 32, 36):
                if len(payload) > offset:
                    inputs.append(payload[offset:])
        for encrypted in inputs:
            plain = _dpapi_unprotect(encrypted)
            if plain is None:
                continue
            for candidate in _candidate_keys(plain):
                if (expected and _matches_hash(candidate, expected)) or (
                    validator is not None and validator(candidate)
                ):
                    return candidate
    return None


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

    class _MemoryBasicInformation(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("PartitionId", wintypes.WORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
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


def _check_memory_block(
    data: bytes,
    block_address: int,
    process: int,
    expected: set[str],
    anchors: list[bytes],
    validator: Callable[[bytes], bool] | None,
    seen: set[bytes],
    budget: list[int],
    anchor_radius: int,
) -> bytes | None:
    def is_valid(candidate: bytes) -> bool:
        if candidate in seen or budget[0] <= 0:
            return False
        seen.add(candidate)
        if expected and _matches_hash(candidate, expected):
            return True
        if validator is not None:
            budget[0] -= 1
            return validator(candidate)
        return False

    for match in HEX_LITERAL_RE.finditer(data):
        candidate = bytes.fromhex(match.group(1)[:64].decode("ascii"))
        if is_valid(candidate):
            return candidate
    for match in HEX_KEY_RE.finditer(data):
        candidate = bytes.fromhex(match.group().decode("ascii"))
        if is_valid(candidate):
            return candidate
    positions: list[int] = []
    lowered = data.lower()
    for anchor in anchors:
        start = 0
        while True:
            found = lowered.find(anchor.lower(), start)
            if found < 0:
                break
            positions.append(found)
            start = found + 1
    for position in positions:
        lower = max(0, position - anchor_radius)
        upper = min(len(data), position + anchor_radius)
        window = data[lower:upper]
        for match in HEX_KEY_RE.finditer(window):
            candidate = bytes.fromhex(match.group().decode("ascii"))
            if is_valid(candidate):
                return candidate
        # Heap allocations are pointer-aligned on 64-bit Windows. Restricting the
        # raw scan to aligned candidates keeps direct SQLCipher validation bounded.
        start = (-lower) % 8
        for index in range(start, max(start, len(window) - 31), 8):
            candidate = window[index : index + 32]
            if is_valid(candidate):
                return candidate
        # SQLCipher keeps key material in separately allocated buffers. Probe
        # pointer-sized fields near a selective anchor and validate the small
        # pointed-to buffers rather than scanning the entire process bytewise.
        kernel32 = ctypes.windll.kernel32
        pointer_start = lower + ((-(block_address + lower)) % 8)
        for pointer_index in range(pointer_start, max(pointer_start, upper - 7), 8):
            pointer = int.from_bytes(data[pointer_index : pointer_index + 8], "little")
            if pointer < 0x10000 or pointer >= (1 << 47) or pointer % 8:
                continue
            pointed = ctypes.create_string_buffer(128)
            bytes_read = ctypes.c_size_t()
            ok = kernel32.ReadProcessMemory(
                process,
                ctypes.c_void_p(pointer),
                pointed,
                128,
                ctypes.byref(bytes_read),
            )
            if not ok or bytes_read.value < 32:
                continue
            pointed_data = pointed.raw[: bytes_read.value]
            for candidate_index in range(0, len(pointed_data) - 31, 8):
                if is_valid(pointed_data[candidate_index : candidate_index + 32]):
                    return pointed_data[candidate_index : candidate_index + 32]
    return None


def recover_from_process_memory(
    account: Account,
    records: list[tuple[str, bytes]],
    validator: Callable[[bytes], bool] | None = None,
    validation_limit: int = 8192,
    anchor_radius: int = 4096,
) -> bytes | None:
    if os.name != "nt":
        return None
    expected = {key_hash for key_hash, _ in records if key_hash}
    anchors: list[bytes] = []
    for _key_hash, blob in records[:2]:
        payload = _first_length_delimited_field(blob)
        anchors.append((payload or blob)[:16])
    anchors.extend(value.encode("ascii") for value in expected)
    database_path = str(account.data_dir / "db_storage" / "contact" / "contact.db")
    try:
        with Path(database_path).open("rb") as database_file:
            database_salt = database_file.read(16)
    except OSError:
        database_salt = b""
    anchors.extend(
        (
            database_salt,
            database_path.encode("utf-8"),
            database_path.encode("utf-16-le"),
            b"contact.db",
            V4_BINARY_SENTINEL,
        )
    )
    anchors = [anchor for anchor in anchors if anchor]
    # The serialized key record is a much more selective anchor than wxid,
    # which can occur hundreds of times in UI and network buffers.
    anchors.append(account.wxid.encode("utf-8"))
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    process_flags = 0x0010 | 0x0400
    readable = {0x04, 0x08}
    seen: set[bytes] = set()
    validation_budget = [validation_limit]
    for pid in _weixin_pids():
        process = kernel32.OpenProcess(process_flags, False, pid)
        if not process:
            continue
        try:
            address = 0
            mbi = _MemoryBasicInformation()
            scanned = 0
            while address < (1 << 47) and scanned < (768 << 20):
                queried = kernel32.VirtualQueryEx(
                    process, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)
                )
                if not queried:
                    break
                base = int(mbi.BaseAddress or 0)
                region_size = int(mbi.RegionSize)
                if (
                    mbi.State == 0x1000
                    and mbi.Type == 0x20000
                    and (mbi.Protect & 0xFF) in readable
                    and not (mbi.Protect & 0x100)
                ):
                    offset = 0
                    overlap = b""
                    while offset < region_size:
                        amount = min(4 << 20, region_size - offset)
                        buffer = ctypes.create_string_buffer(amount)
                        bytes_read = ctypes.c_size_t()
                        ok = kernel32.ReadProcessMemory(
                            process,
                            ctypes.c_void_p(base + offset),
                            buffer,
                            amount,
                            ctypes.byref(bytes_read),
                        )
                        if ok and bytes_read.value:
                            block = overlap + buffer.raw[: bytes_read.value]
                            block_address = base + offset - len(overlap)
                            candidate = _check_memory_block(
                                block,
                                block_address,
                                process,
                                expected,
                                anchors,
                                validator,
                                seen,
                                validation_budget,
                                anchor_radius,
                            )
                            if candidate:
                                return candidate
                            overlap = block[-1536:]
                            scanned += bytes_read.value
                        offset += amount
                address = base + max(region_size, 4096)
        finally:
            kernel32.CloseHandle(process)
    return None


def recover_database_key(
    account: Account, data_root: Path, manual_key_file: Path | None = None
) -> bytes:
    if manual_key_file:
        return load_manual_key(manual_key_file)
    records = load_key_records(account, data_root)
    from .database import (
        EncryptedDatabase,
        normalize_sqlcipher4_key,
        quick_validate_sqlcipher_key,
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

    def validates(candidate: bytes) -> bool:
        if not quick_validate_sqlcipher_key(contact_database, candidate):
            return False
        return confirms(candidate)

    def validates_raw(candidate: bytes) -> bool:
        if not quick_validate_sqlcipher_key(
            contact_database, candidate, include_passphrase_kdf=False
        ):
            return False
        return confirms(candidate)

    def normalizes(candidate: bytes) -> bytes | None:
        normalized = normalize_sqlcipher4_key(contact_database, candidate)
        if normalized is None or not confirms(candidate):
            return None
        # Keep the original master password. EncryptedDatabase derives a
        # different post-KDF key for every database's salt.
        return candidate

    from .weflow_key import recover_with_wx_key_dll

    key = recover_with_wx_key_dll(
        _weixin_pids(),
        normalizes,
        status=lambda message: print(f"  {message}"),
        pid_provider=_weixin_pids,
    )
    if key:
        return key

    key = recover_with_dpapi(records, validates)
    if key:
        return key
    key = recover_from_process_memory(
        account,
        records,
        validates_raw,
        validation_limit=2048,
        anchor_radius=2048,
    )
    if key:
        return key
    key = recover_from_process_memory(
        account,
        records,
        validates,
        validation_limit=32,
        anchor_radius=512,
    )
    if key:
        return key
    raise KeyRecoveryError(
        "自动获取数据库密钥失败。请保持目标账号已登录后重试；"
        "也可以用 --key-file 指定只含 64 位十六进制密钥的本地文件。"
    )
